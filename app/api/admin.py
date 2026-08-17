"""Token administration — create, list, revoke.

Gated on `require_admin_key`, which accepts STATIC `SERVICE_API_KEYS` only. A token
issued here can never mint another, so a leaked consumer token cannot escalate.

Deliberately excluded from the OpenAPI schema: the public docs are for consumers of
`/search` and friends, and listing an admin surface there invites probing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import SettingsDep
from app.logging_setup import get_logger
from app.models import (
    TokenCreatedResponse,
    TokenCreateRequest,
    TokenInfo,
    TokenListResponse,
)
from app.security import require_admin_key
from app.tokens import TokenStore, TokenStoreUnavailable

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)

_NO_STORE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail={
        "error": "token store unavailable",
        "hint": "tokens are stored in Redis; check CACHE_ENABLED and REDIS_URL",
    },
)


def _store(request: Request) -> TokenStore:
    store: TokenStore | None = getattr(request.app.state, "token_store", None)
    if store is None or not store.enabled:
        raise _NO_STORE
    return store


def _as_info(token) -> TokenInfo:
    return TokenInfo(
        id=token.id,
        name=token.name,
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
    )


@router.post(
    "/tokens",
    response_model=TokenCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new API token",
)
async def create_token(
    payload: TokenCreateRequest,
    request: Request,
    admin: Annotated[str, Depends(require_admin_key)],
) -> TokenCreatedResponse:
    store = _store(request)
    try:
        token = await store.create(payload.name, ttl_days=payload.expires_in_days)
    except TokenStoreUnavailable as exc:
        # Fail loudly. A write that silently did nothing would hand out a credential
        # that never works.
        log.error("admin.token_create_failed", error=str(exc))
        raise _NO_STORE from exc

    log.info("admin.token_created", by=admin, token_id=token.id, name=token.name)
    return TokenCreatedResponse(**_as_info(token).model_dump(), secret=token.secret or "")


@router.get(
    "/tokens",
    response_model=TokenListResponse,
    summary="List live tokens (metadata only)",
)
async def list_tokens(
    request: Request,
    settings: SettingsDep,
    admin: Annotated[str, Depends(require_admin_key)],
) -> TokenListResponse:
    store = _store(request)
    try:
        tokens = await store.list_tokens()
    except TokenStoreUnavailable as exc:
        raise _NO_STORE from exc

    return TokenListResponse(
        tokens=[_as_info(t) for t in tokens],
        count=len(tokens),
        static_keys=len(settings.service_api_keys),
    )


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token immediately",
)
async def revoke_token(
    token_id: str,
    request: Request,
    admin: Annotated[str, Depends(require_admin_key)],
) -> None:
    store = _store(request)
    try:
        existed = await store.revoke(token_id)
    except TokenStoreUnavailable as exc:
        raise _NO_STORE from exc

    if not existed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no such token", "id": token_id},
        )
    log.info("admin.token_revoked", by=admin, token_id=token_id)
