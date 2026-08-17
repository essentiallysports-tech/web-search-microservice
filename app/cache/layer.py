"""The cache read path: L1 → L2 → coalesced compute.

`get_or_compute` is the only entry point callers need. Four layers stop the same
work being paid for twice:

1. L1 (process memory) — no network hop for hot keys.
2. L2 (Redis) — shared across replicas; the source of truth.
3. In-process single-flight — duplicates inside one worker collapse to one call.
4. Redis lock — duplicates ACROSS workers collapse too; non-leaders wait briefly
   for the leader's result to land in L2.

Every layer fails open: an unreachable Redis degrades this to "call the provider",
which is what would happen with no cache at all.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.cache.keys import lock_key
from app.cache.local import MISS, TTLCache
from app.cache.redis_cache import LockOutcome, RedisCache
from app.cache.singleflight import InProcessSingleFlight
from app.common.metrics import cache_events
from app.config import Settings
from app.logging_setup import get_logger
from app.models import CacheState

log = get_logger(__name__)

# Follower poll cadence while waiting on a leader. Starts tight so a fast provider
# call is picked up almost immediately, then backs off so a slow one doesn't cost a
# poll every 20ms for ten seconds.
_POLL_INITIAL_S = 0.02
_POLL_MAX_S = 0.25
_POLL_GROWTH = 1.6

TTLSpec = int | Callable[[Any], int]


class CacheLayer:
    def __init__(
        self,
        settings: Settings,
        redis_cache: RedisCache | None,
        *,
        local: TTLCache | None = None,
    ) -> None:
        self.settings = settings
        self._redis = redis_cache
        self._local = (
            local
            if local is not None
            else TTLCache(settings.local_cache_size, settings.local_cache_ttl_s)
        )
        self._singleflight = InProcessSingleFlight()

    @property
    def enabled(self) -> bool:
        return self.settings.cache_enabled and self._redis is not None

    @property
    def redis(self) -> RedisCache | None:
        """The underlying L2, for components that share the connection."""
        return self._redis

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]],
        *,
        ttl: TTLSpec,
        namespace: str,
        bypass: bool = False,
    ) -> tuple[Any, CacheState]:
        if not self.enabled:
            return await compute(), CacheState.BYPASS

        if bypass:
            # Skips READING the cache but still refreshes it: the caller already paid
            # for a fresh result, so everyone else should benefit.
            cache_events.labels(namespace, "bypass").inc()
            value = await compute()
            await self._store(key, value, ttl, namespace)
            return value, CacheState.BYPASS

        local_hit = self._local.get(key)
        if local_hit is not MISS:
            cache_events.labels(namespace, "hit").inc()
            return local_hit, CacheState.HIT

        cached = await self._redis.get(key, namespace=namespace)
        if cached is not None:
            self._local.set(key, cached)
            cache_events.labels(namespace, "hit").inc()
            return cached, CacheState.HIT

        # State travels back with the value rather than via an attribute, so
        # concurrent keys can't clobber each other's bookkeeping.
        (value, state), joined = await self._singleflight.do(
            key, lambda: self._compute_with_lock(key, compute, ttl, namespace)
        )
        if joined:
            state = CacheState.COALESCED

        cache_events.labels(namespace, state.value).inc()
        return value, state

    # ---------------------------------------------------- direct read/write
    # For values written as a side effect rather than computed on demand — the
    # negative cache being the motivating case.

    async def get(self, key: str, *, namespace: str) -> Any | None:
        if not self.enabled:
            return None

        local_hit = self._local.get(key)
        if local_hit is not MISS:
            cache_events.labels(namespace, "hit").inc()
            return local_hit

        value = await self._redis.get(key, namespace=namespace)
        if value is not None:
            self._local.set(key, value)
            cache_events.labels(namespace, "hit").inc()
        return value

    async def set(self, key: str, value: Any, *, ttl: int, namespace: str) -> None:
        if not self.enabled or ttl <= 0:
            return
        await self._store(key, value, ttl, namespace)

    # ------------------------------------------------------------- internals

    async def _compute_with_lock(
        self,
        key: str,
        compute: Callable[[], Awaitable[Any]],
        ttl: TTLSpec,
        namespace: str,
    ) -> tuple[Any, CacheState]:
        token = uuid.uuid4().hex
        lock = lock_key(key)
        outcome = await self._redis.try_lock(
            lock, token, self.settings.singleflight_lock_ttl_s
        )

        if outcome is LockOutcome.ACQUIRED:
            try:
                value = await compute()
                # Store BEFORE releasing: a follower seeing the lock vanish with no
                # value yet would start its own duplicate call.
                await self._store(key, value, ttl, namespace)
            finally:
                await self._redis.unlock(lock, token)
            return value, CacheState.MISS

        if outcome is LockOutcome.HELD:
            # Another worker is computing this; wait for its result to land in L2.
            waited = await self._await_leader(key, namespace)
            if waited is not None:
                self._local.set(key, waited)
                return waited, CacheState.COALESCED
            log.info("cache.leader_wait_timeout", key=key, namespace=namespace)

        # Either the leader never delivered, or Redis is UNAVAILABLE and there is no
        # leader to wait for. The latter skips the wait entirely — waiting on a dead
        # Redis would add the full single-flight timeout to an already-degraded
        # request, which is where a 13-second stall came from.
        value = await compute()
        await self._store(key, value, ttl, namespace)
        return value, CacheState.MISS

    async def _await_leader(self, key: str, namespace: str) -> Any | None:
        deadline = time.monotonic() + self.settings.singleflight_wait_s
        delay = _POLL_INITIAL_S
        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            if not self._redis.available:
                # Redis died mid-wait; no result is coming.
                return None
            value = await self._redis.get(key, namespace=namespace)
            if value is not None:
                return value
            delay = min(delay * _POLL_GROWTH, _POLL_MAX_S)
        return None

    async def _store(self, key: str, value: Any, ttl: TTLSpec, namespace: str) -> None:
        # TTL may depend on the value — a degraded result must not live as long as a
        # clean one.
        seconds = ttl(value) if callable(ttl) else ttl
        if seconds <= 0:
            return
        await self._redis.set(key, value, seconds, namespace=namespace)
        self._local.set(key, value)

    # ------------------------------------------------------------------ ops

    async def health(self) -> bool:
        if self._redis is None:
            return False
        return await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
        self._local.clear()

    def invalidate_local(self, key: str) -> None:
        self._local.delete(key)
