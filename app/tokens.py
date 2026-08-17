"""Dynamic API tokens, stored in Redis.

`SERVICE_API_KEYS` is a static CSV read at boot, which cannot back a self-service UI:
issuing or revoking one means editing `.env` and restarting, and there is nowhere to
record who owns a token or when it was made. This module is the dynamic half.

Both paths stay live, with different jobs:

    SERVICE_API_KEYS   few, static, survives a Redis outage. Admin + break-glass.
    token store here   many, revocable, has metadata. Issued to consuming apps.

Two properties worth not breaking:

- **Only a hash is stored.** The secret is returned once at creation and never again,
  so a dumped Redis database yields no working credentials. Verification hashes the
  presented token and looks the digest up as a key, so no comparison against a stored
  secret happens anywhere.
- **Admin endpoints accept static keys only.** A token minted here can never mint
  another, so leaking a consumer's token cannot escalate into issuing more.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from redis.exceptions import RedisError

from app.cache.redis_cache import RedisCache
from app.logging_setup import get_logger

log = get_logger(__name__)

#: Prefix on every issued secret. Makes a leaked token greppable in logs and source,
#: and lets a caller tell our credential apart from a provider's.
TOKEN_PREFIX = "esw_"

#: Bytes of entropy in the secret. 32 gives ~43 url-safe chars, well past guessing.
_SECRET_BYTES = 32

#: Public id length. Short enough to read aloud, long enough not to collide.
_ID_BYTES = 6


def generate_secret() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def hash_secret(secret: str) -> str:
    """Digest used as the lookup key.

    sha256 rather than a password KDF on purpose: these are 256-bit random secrets,
    not user-chosen passwords, so there is no dictionary to slow down — and this runs
    on every authenticated request.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ApiToken:
    """A token's metadata. Never carries the secret."""

    id: str
    name: str
    created_at: float
    expires_at: float | None = None
    last_used_at: float | None = None
    #: Only set on the response to a create call, never stored or re-read.
    secret: str | None = field(default=None, compare=False)

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def to_record(self, secret_hash: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
            "hash": secret_hash,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ApiToken:
        return cls(
            id=str(record.get("id", "")),
            name=str(record.get("name", "")),
            created_at=float(record.get("created_at") or 0.0),
            expires_at=record.get("expires_at"),
            last_used_at=record.get("last_used_at"),
        )


class TokenStoreUnavailable(RuntimeError):
    """Redis could not be reached. Admin writes fail loudly rather than silently."""


class TokenStore:
    """Redis-backed token storage.

    Two keys per token, because the two access patterns want different shapes:

        {ns}:tok:{sha256}  -> the record, for O(1) verification on every request
        {ns}:tokens        -> HASH id -> record, for listing and revoking by id

    Verification must not be a scan, and listing must not require knowing secrets.
    """

    def __init__(self, redis_cache: RedisCache | None, *, version: str = "v1") -> None:
        self._redis = redis_cache
        self._ns = f"wss:{version}"

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    def _lookup_key(self, secret_hash: str) -> str:
        return f"{self._ns}:tok:{secret_hash}"

    @property
    def _index_key(self) -> str:
        return f"{self._ns}:tokens"

    # ------------------------------------------------------------------ writes

    async def create(
        self, name: str, *, ttl_days: int | None = None
    ) -> ApiToken:
        """Mint a token. The returned object is the ONLY time the secret exists."""
        if self._redis is None:
            raise TokenStoreUnavailable("no Redis configured; cannot issue tokens")

        secret = generate_secret()
        digest = hash_secret(secret)
        now = time.time()
        token = ApiToken(
            id=secrets.token_hex(_ID_BYTES),
            name=name.strip() or "unnamed",
            created_at=now,
            expires_at=(now + ttl_days * 86400) if ttl_days else None,
            secret=secret,
        )
        record = token.to_record(digest)

        client = self._redis._client
        try:
            pipe = client.pipeline()
            # The lookup entry carries a Redis TTL as well as an explicit
            # `expires_at`. Belt and braces: the TTL reclaims memory without a sweeper,
            # and the timestamp means an expired token is still refused if the TTL has
            # not fired yet.
            blob = self._encode(record)
            if token.expires_at is not None:
                pipe.set(self._lookup_key(digest), blob, ex=int(ttl_days * 86400))
            else:
                pipe.set(self._lookup_key(digest), blob)
            pipe.hset(self._index_key, token.id, blob)
            await pipe.execute()
        except RedisError as exc:
            log.error("tokens.create_failed", error=repr(exc))
            raise TokenStoreUnavailable(f"could not write token: {exc!r}") from exc

        log.info(
            "tokens.created", token_id=token.id, name=token.name,
            expires_at=token.expires_at,
        )
        return token

    async def revoke(self, token_id: str) -> bool:
        """Delete a token by public id. Returns False if it was already gone."""
        if self._redis is None:
            raise TokenStoreUnavailable("no Redis configured; cannot revoke tokens")

        client = self._redis._client
        try:
            blob = await client.hget(self._index_key, token_id)
            if blob is None:
                return False
            record = self._decode(blob) or {}
            digest = record.get("hash")

            pipe = client.pipeline()
            pipe.hdel(self._index_key, token_id)
            if digest:
                # Delete the lookup entry too, or the secret keeps authenticating.
                pipe.delete(self._lookup_key(str(digest)))
            await pipe.execute()
        except RedisError as exc:
            log.error("tokens.revoke_failed", token_id=token_id, error=repr(exc))
            raise TokenStoreUnavailable(f"could not revoke token: {exc!r}") from exc

        log.info("tokens.revoked", token_id=token_id)
        return True

    # ------------------------------------------------------------------- reads

    async def list_tokens(self) -> list[ApiToken]:
        """Every live token, newest first. Expired entries are pruned as they surface."""
        if self._redis is None:
            raise TokenStoreUnavailable("no Redis configured; cannot list tokens")

        client = self._redis._client
        try:
            raw = await client.hgetall(self._index_key)
        except RedisError as exc:
            log.error("tokens.list_failed", error=repr(exc))
            raise TokenStoreUnavailable(f"could not list tokens: {exc!r}") from exc

        now = time.time()
        tokens: list[ApiToken] = []
        stale: list[str] = []
        for blob in (raw or {}).values():
            record = self._decode(blob)
            if not record:
                continue
            token = ApiToken.from_record(record)
            if token.is_expired(now):
                # The lookup key expires on its own via TTL; the index entry does not,
                # so drop it here rather than growing the hash forever.
                stale.append(token.id)
                continue
            tokens.append(token)

        if stale:
            try:
                await client.hdel(self._index_key, *stale)
                log.info("tokens.pruned_expired", count=len(stale))
            except RedisError:
                pass  # cosmetic cleanup; never fail a read over it

        tokens.sort(key=lambda t: t.created_at, reverse=True)
        return tokens

    async def verify(self, secret: str) -> ApiToken | None:
        """Resolve a presented secret to its token, or None.

        On the hot path for every authenticated request, so this is one Redis GET
        against a digest — no scan, and no comparison against a stored secret.

        Returns None rather than raising when Redis is unreachable: the static
        SERVICE_API_KEYS path must keep working through a cache outage.
        """
        if self._redis is None or not secret:
            return None
        if not secret.startswith(TOKEN_PREFIX):
            # Not one of ours; skip the round trip. Static keys are checked separately.
            return None

        try:
            blob = await self._redis._client.get(self._lookup_key(hash_secret(secret)))
        except RedisError as exc:
            log.warning("tokens.verify_unavailable", error=repr(exc))
            return None
        if blob is None:
            return None

        record = self._decode(blob)
        if not record:
            return None
        token = ApiToken.from_record(record)
        if token.is_expired():
            # TTL had not fired yet. Refuse anyway.
            log.info("tokens.expired_presented", token_id=token.id)
            return None
        return token

    async def touch(self, token: ApiToken) -> None:
        """Record last use, best-effort.

        Deliberately fire-and-forget and NOT awaited on the critical path by callers
        that care about latency: it is an ops nicety, not an authorization input.
        Only writes when the stored value is more than a minute stale, so a hot token
        does not mean a Redis write per request.
        """
        if self._redis is None:
            return
        now = time.time()
        if token.last_used_at is not None and now - token.last_used_at < 60:
            return
        try:
            blob = await self._redis._client.hget(self._index_key, token.id)
            if blob is None:
                return
            record = self._decode(blob) or {}
            record["last_used_at"] = now
            updated = self._encode(record)
            pipe = self._redis._client.pipeline()
            pipe.hset(self._index_key, token.id, updated)
            digest = record.get("hash")
            if digest:
                pipe.set(self._lookup_key(str(digest)), updated, keepttl=True)
            await pipe.execute()
        except RedisError:
            pass  # never let bookkeeping fail a request

    # --------------------------------------------------------------- internals

    @staticmethod
    def _encode(record: dict[str, Any]) -> bytes:
        import orjson

        return orjson.dumps(record)

    @staticmethod
    def _decode(blob: bytes | None) -> dict[str, Any] | None:
        if not blob:
            return None
        import orjson

        try:
            value = orjson.loads(blob)
        except orjson.JSONDecodeError:
            log.warning("tokens.corrupt_record")
            return None
        return value if isinstance(value, dict) else None
