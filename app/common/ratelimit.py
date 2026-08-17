"""Per-consumer rate limiting, weighted by what each endpoint costs.

A flat requests-per-minute cap treats a cached `/search` and a `/research` call as
equivalent, when one is free and the other spends LLM tokens. Each endpoint declares a
cost weight and the limit is a budget of units per minute, so a consumer can make many
cheap calls or a few expensive ones.

Counters live in Redis so the limit is shared across replicas; an in-process limiter
would give each replica its own full budget.

Fails open — a cache outage must not also become an availability outage.

KNOWN GAP: this does not consult the Redis circuit breaker, so a Redis outage costs a
socket timeout on every request here while the cache layer short-circuits correctly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.exceptions import RedisError

from app.cache.redis_cache import RedisCache
from app.common.metrics import rate_limit_events
from app.config import Settings
from app.logging_setup import get_logger

log = get_logger(__name__)

#: Relative cost per endpoint. `/research` is an order of magnitude dearer because it
#: is the only path that spends LLM tokens.
ENDPOINT_COST: dict[str, int] = {
    "/search": 1,
    "/extract": 3,
    "/search_and_extract": 4,
    "/research": 10,
}
DEFAULT_COST = 1


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_after_s: int
    limit: int


class RateLimiter:
    def __init__(self, settings: Settings, redis_cache: RedisCache | None) -> None:
        self.settings = settings
        self._redis = redis_cache

    @property
    def enabled(self) -> bool:
        return (
            self.settings.rate_limit_enabled
            and self._redis is not None
            and self.settings.rate_limit_per_minute > 0
        )

    async def check(self, caller: str, endpoint: str) -> RateLimitResult:
        limit = self.settings.rate_limit_per_minute
        if not self.enabled:
            return RateLimitResult(True, limit, 0, limit)

        cost = ENDPOINT_COST.get(endpoint, DEFAULT_COST)
        # Fixed window: two Redis commands instead of the sorted-set bookkeeping a
        # sliding window needs. Costs a burst of up to 2x the limit across a window
        # boundary, which is fine for protecting upstreams.
        window = int(time.time() // 60)
        key = f"wss:{self.settings.cache_version}:rl:{caller}:{window}"

        try:
            used = await self._incr(key, cost)
        except RedisError as exc:
            # Fail open: an unreachable Redis must not become an outage.
            log.warning("ratelimit.unavailable", error=repr(exc))
            rate_limit_events.labels("error").inc()
            return RateLimitResult(True, limit, 0, limit)

        reset_after = 60 - int(time.time() % 60)
        if used > limit:
            rate_limit_events.labels("throttled").inc()
            log.info(
                "ratelimit.throttled",
                caller=caller,
                endpoint=endpoint,
                used=used,
                limit=limit,
            )
            return RateLimitResult(False, 0, reset_after, limit)

        rate_limit_events.labels("allowed").inc()
        return RateLimitResult(True, max(0, limit - used), reset_after, limit)

    async def _incr(self, key: str, cost: int) -> int:
        client = self._redis._client  # pipeline needs the raw client
        # INCRBY + EXPIRE in one round trip. EXPIRE is unconditional rather than
        # NX-guarded: re-setting a 120s TTL is harmless and guarantees a window key
        # can never outlive its window.
        pipe = client.pipeline()
        pipe.incrby(key, cost)
        pipe.expire(key, 120)
        used, _ = await pipe.execute()
        return int(used)
