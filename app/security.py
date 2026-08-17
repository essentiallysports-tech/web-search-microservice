"""Consumer authentication.

Two credential paths, checked in this order:

1. `SERVICE_API_KEYS` — a static CSV from `.env`. Few, no metadata, survives a Redis
   outage. This is the ADMIN credential and the break-glass one.
2. The Redis token store (`app/tokens.py`) — many, revocable, with an owner name and
   optional expiry. Issued to consuming apps through the admin UI.

Static first, deliberately: it costs no round trip, and it is the path that has to keep
working when Redis is down.

Admin endpoints require a STATIC key specifically (`require_admin_key`). A token issued
from the store can never mint another, so a leaked consumer token cannot escalate into
issuing more.
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request, Response, status

from app.common.ratelimit import RateLimiter
from app.config import Settings
from app.tokens import ApiToken, TokenStore

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="missing or invalid API key",
    headers={"WWW-Authenticate": "ApiKey"},
)

_FORBIDDEN_NOT_ADMIN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail={
        "error": "admin credential required",
        "hint": "this endpoint accepts only a key from SERVICE_API_KEYS, not an issued token",
    },
)


def caller_identity(key: str) -> str:
    """A stable, non-secret identifier for a static API key.

    A key PREFIX is not usable here: keys routinely share one (`sk-proj-…`, a company
    prefix, or plain `svc-key-1`/`svc-key-2`), and two that collide would share a
    rate-limit budget and be indistinguishable in logs.
    """
    return "key:" + hashlib.blake2b(key.encode("utf-8"), digest_size=6).hexdigest()


def token_identity(token: ApiToken) -> str:
    """Identity for an issued token.

    Uses the token's own public id, so logs and the rate limiter agree with what the
    admin UI shows — you can read a throttled identity straight off the dashboard.
    """
    return f"token:{token.id}"


def match_static_key(settings: Settings, presented: str | None) -> str | None:
    """The caller identity if `presented` is a static key, else None.

    Constant-time comparison so a wrong key leaks nothing through timing.
    """
    if not presented:
        return None
    for known in settings.service_api_keys:
        if secrets.compare_digest(presented, known):
            return caller_identity(known)
    return None


def resolve_key(settings: Settings, presented: str | None) -> str:
    """Static-only resolution. Kept for callers with no token store to consult."""
    if not settings.auth_enabled:
        return "anonymous"
    identity = match_static_key(settings, presented)
    if identity is None:
        raise _UNAUTHORIZED
    return identity


async def resolve_caller(
    settings: Settings, store: TokenStore | None, presented: str | None
) -> str:
    """Full resolution: static keys, then the dynamic token store."""
    if not settings.auth_enabled:
        return "anonymous"
    if not presented:
        raise _UNAUTHORIZED

    identity = match_static_key(settings, presented)
    if identity is not None:
        return identity

    if store is not None:
        token = await store.verify(presented)
        if token is not None:
            await store.touch(token)
            return token_identity(token)

    raise _UNAUTHORIZED


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    # From app.state, not the cached global — see api/deps.get_app_settings.
    return await resolve_caller(
        request.app.state.settings,
        getattr(request.app.state, "token_store", None),
        x_api_key,
    )


async def require_admin_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Admin gate for the token endpoints — static keys only.

    Separate from `require_api_key` so that issuing credentials is strictly more
    privileged than using them. Returns 403 rather than 401 for a VALID token that
    simply isn't an admin, so the caller can tell "wrong credential" from "not allowed".
    """
    settings: Settings = request.app.state.settings

    if not settings.auth_enabled:
        # Dev convenience, and it mirrors the rest of the service. Refusing here would
        # make the admin UI untestable locally; `_validate_startup` is what warns about
        # auth being off in prod.
        return "anonymous-admin"

    if not x_api_key:
        raise _UNAUTHORIZED

    identity = match_static_key(settings, x_api_key)
    if identity is not None:
        return identity

    store: TokenStore | None = getattr(request.app.state, "token_store", None)
    if store is not None and await store.verify(x_api_key) is not None:
        raise _FORBIDDEN_NOT_ADMIN

    raise _UNAUTHORIZED


async def authorized_caller(
    request: Request,
    response: Response,
    caller: str = Depends(require_api_key),
) -> str:
    """Authenticate, then charge the caller's rate-limit budget.

    Order matters: resolve the key before spending anything, or an unauthenticated
    request could consume another consumer's budget.
    """
    limiter: RateLimiter = request.app.state.rate_limiter
    result = await limiter.check(caller, request.url.path)

    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)

    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate limit exceeded",
                "limit_per_minute": result.limit,
                "retry_after_s": result.reset_after_s,
            },
            headers={
                "Retry-After": str(result.reset_after_s),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
    return caller
