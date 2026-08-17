"""Redis (L2) cache wrapper.

Every method fails open. A Redis outage degrades this service to "uncached but
working", never to "down": losing the cache is a cost problem, not a correctness one.
Errors are swallowed, counted, and logged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache.codec import Codec, CorruptCacheValue
from app.common.circuit import CircuitBreaker
from app.common.metrics import cache_events
from app.logging_setup import get_logger

log = get_logger(__name__)


class LockOutcome(StrEnum):
    """Why a single-flight lock attempt ended as it did.

    HELD means waiting for a leader is worthwhile. UNAVAILABLE means there is no leader
    to wait for, so waiting would only add latency to an already-degraded request.
    """

    ACQUIRED = "acquired"
    HELD = "held"
    UNAVAILABLE = "unavailable"

# Compare-and-delete: only release a lock we still own. Without the token check, a
# leader whose lock had expired could delete one a second worker legitimately holds.
_UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisCache:
    def __init__(
        self,
        client: Redis,
        codec: Codec,
        *,
        fail_threshold: int = 5,
        reset_after_s: float = 60.0,
    ) -> None:
        self._client = client
        self._codec = codec
        self._unlock = client.register_script(_UNLOCK_LUA)
        # A down Redis must not add a socket timeout to every operation. After a few
        # failures the breaker opens and cache calls become no-ops until worth probing.
        self._breaker = CircuitBreaker(
            "redis", fail_threshold=fail_threshold, reset_after_s=reset_after_s
        )

    @property
    def available(self) -> bool:
        """False while the breaker is open — callers should not wait on Redis."""
        return self._breaker.allows()

    async def get(self, key: str, *, namespace: str = "-") -> Any | None:
        if not self._breaker.allows():
            return None

        try:
            blob = await self._client.get(key)
        except RedisError as exc:
            await self._breaker.record_failure()
            cache_events.labels(namespace, "error").inc()
            log.warning("cache.redis_get_failed", key=key, error=repr(exc))
            return None
        await self._breaker.record_success()

        if blob is None:
            return None

        try:
            return self._codec.decode(blob)
        except CorruptCacheValue as exc:
            # Treat as a miss and drop it so it stops costing reads.
            cache_events.labels(namespace, "error").inc()
            log.warning("cache.corrupt_value", key=key, error=str(exc))
            await self.delete(key)
            return None

    async def set(self, key: str, value: Any, ttl_s: int, *, namespace: str = "-") -> bool:
        if ttl_s <= 0 or not self._breaker.allows():
            return False
        try:
            await self._client.set(key, self._codec.encode(value), ex=ttl_s)
        except RedisError as exc:
            await self._breaker.record_failure()
            cache_events.labels(namespace, "error").inc()
            log.warning("cache.redis_set_failed", key=key, error=repr(exc))
            return False
        await self._breaker.record_success()
        return True

    async def delete(self, key: str) -> None:
        if not self._breaker.allows():
            return
        try:
            await self._client.delete(key)
        except RedisError as exc:
            await self._breaker.record_failure()
            log.warning("cache.redis_delete_failed", key=key, error=repr(exc))

    # ----------------------------------------------------------- coalescing

    async def try_lock(self, key: str, token: str, ttl_s: int) -> LockOutcome:
        """Attempt to become the single computer for `key`."""
        if not self._breaker.allows():
            return LockOutcome.UNAVAILABLE
        try:
            acquired = bool(await self._client.set(key, token, nx=True, ex=ttl_s))
        except RedisError as exc:
            await self._breaker.record_failure()
            log.warning("cache.lock_failed", key=key, error=repr(exc))
            return LockOutcome.UNAVAILABLE
        await self._breaker.record_success()
        return LockOutcome.ACQUIRED if acquired else LockOutcome.HELD

    async def unlock(self, key: str, token: str) -> None:
        if not self._breaker.allows():
            return
        try:
            await self._unlock(keys=[key], args=[token])
        except RedisError as exc:
            await self._breaker.record_failure()
            log.warning("cache.unlock_failed", key=key, error=repr(exc))

    # ---------------------------------------------------------------- health

    async def ping(self) -> bool:
        # Deliberately bypasses the breaker — this is how we find out Redis is back,
        # and only the health endpoint calls it.
        try:
            alive = bool(await self._client.ping())
        except RedisError:
            return False
        if alive:
            await self._breaker.record_success()
        return alive

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except (RedisError, AttributeError) as exc:
            log.warning("cache.close_failed", error=repr(exc))
