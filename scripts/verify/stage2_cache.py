"""Stage 2 — caching and single-flight against live Valkey + the search API.

  python scripts/verify/stage2_cache.py

Every cache miss is a billed search call, so the hit rate measured here IS the cost
structure rather than a latency nicety — and the coalescing check is the difference
between one paid call and eight.

Requires: Redis/Valkey running (dev compose overlay) and SERPER_API_KEY.
"""

from __future__ import annotations

import asyncio
import sys
import time

from _harness import (
    LOCAL_REDIS,
    Timer,
    check,
    info,
    preflight,
    section,
    spend_notice,
    spend_report,
    summary,
)
from redis.asyncio import Redis

from app.cache.codec import Codec
from app.cache.layer import CacheLayer
from app.cache.redis_cache import RedisCache
from app.config import Settings
from app.http_client import build_client, set_client
from app.logging_setup import configure_logging
from app.models import Freshness
from app.search.serper import SerperProvider
from app.services.search_service import SearchService

configure_logging("ERROR", as_json=False)
settings = Settings(
    REDIS_URL=LOCAL_REDIS,
    CACHE_ENABLED=True,
    CACHE_VERSION="verify",
    SEARCH_TIMEOUT_S=20.0,
)

QUERY = "python type hints guide"


def build(raw: Redis) -> SearchService:
    cache = CacheLayer(
        settings,
        RedisCache(raw, Codec(min_compress_bytes=settings.cache_compress_min_bytes)),
    )
    return SearchService(settings, [SerperProvider(settings)], cache)


async def main() -> int:
    client = build_client(settings)
    set_client(client)
    raw = Redis.from_url(settings.redis_url, decode_responses=False)

    keys = [k async for k in raw.scan_iter(match="wss:verify:*")]
    if keys:
        await raw.delete(*keys)
    info(f"cleared {len(keys)} stale keys")

    service = build(raw)

    try:
        section("cold miss then warm hit")
        with Timer() as cold:
            first = await service.search(QUERY, count=5)
        with Timer() as warm:
            second = await service.search(QUERY, count=5)

        info(f"cold {cold.elapsed_ms:.0f}ms -> warm {warm.elapsed_ms:.1f}ms "
             f"({cold.elapsed_ms / max(warm.elapsed_ms, 0.01):.0f}x)")
        check("first is a miss", first.cache.value == "miss", first.cache.value)
        check("second is a hit", second.cache.value == "hit", second.cache.value)
        check("warm hit under 50ms", warm.elapsed_ms < 50, f"{warm.elapsed_ms:.1f}ms")
        check(
            "cached content matches",
            [r.url for r in first.results] == [r.url for r in second.results],
        )

        section("L2 serves a cold process (simulating a second replica)")
        cold_service = build(raw)
        with Timer() as l2:
            third = await cold_service.search(QUERY, count=5)
        check("cold L1 still hits via Redis", third.cache.value == "hit", third.cache.value)
        info(f"L2 hit: {l2.elapsed_ms:.0f}ms  (baseline ~3ms)")

        section("query normalization merges near-duplicates")
        variant = await service.search("  Python  TYPE hints guide? ", count=5)
        check("punctuation/case/spacing variant hits", variant.cache.value == "hit",
              variant.cache.value)

        section("key sensitivity (each must MISS)")
        for label, kwargs in [
            ("different count", {"count": 10}),
            ("different lang", {"count": 5, "lang": "de"}),
            ("different freshness", {"count": 5, "freshness": Freshness.WEEK}),
        ]:
            outcome = await service.search(QUERY, **kwargs)
            check(label, outcome.cache.value == "miss", outcome.cache.value)

        section("single-flight: concurrent duplicates collapse")
        unique_query = f"single flight probe {int(time.time())}"
        outcomes = await asyncio.gather(*[service.search(unique_query, count=5) for _ in range(8)])
        states = [o.cache.value for o in outcomes]
        check("exactly one upstream call", states.count("miss") == 1,
              f"miss={states.count('miss')} coalesced={states.count('coalesced')}")
        check("the rest coalesced", states.count("coalesced") == 7)

        section("bypass_cache refreshes rather than poisons")
        bypassed = await service.search(QUERY, count=5, bypass_cache=True)
        after = await service.search(QUERY, count=5)
        check("bypass reported", bypassed.cache.value == "bypass", bypassed.cache.value)
        check("next caller gets the refreshed entry", after.cache.value == "hit")

        section("stored payloads")
        stored = sorted([k async for k in raw.scan_iter(match="wss:verify:*")])
        total = 0
        for key in stored:
            blob = await raw.get(key)
            ttl = await raw.ttl(key)
            total += len(blob)
            # Computed outside the f-string: backslash escapes in f-string
            # expressions are a syntax error before Python 3.12.
            frame = "zstd" if blob[:1] == b"\x01" else "raw "
            info(f"{key.decode()[-24:]:26} {frame} {len(blob):6}B ttl={ttl}s")
        check("no lock keys leaked", not any(b":lock" in k for k in stored))
        check("TTLs are set", total > 0)

        section("compression on a page-sized payload")
        codec = Codec(min_compress_bytes=1024)
        page = {"markdown": "Extracted article prose about caching and retrieval. " * 400}
        raw_size = len(Codec(min_compress_bytes=10**9).encode(page))
        packed = len(codec.encode(page))
        info(f"{raw_size:,}B -> {packed:,}B ({raw_size / packed:.0f}x)")
        check("compresses at least 3x", raw_size / packed >= 3)

        section("fail-open: Redis unreachable")
        broken = CacheLayer(
            settings,
            RedisCache(
                Redis.from_url("redis://127.0.0.1:6399/0", socket_connect_timeout=1),
                Codec(),
                fail_threshold=2,
            ),
        )
        broken_service = SearchService(settings, [SerperProvider(settings)], broken)
        with Timer() as degraded:
            outcome = await broken_service.search("fail open probe", count=3)
        info(f"served in {degraded.elapsed_ms:.0f}ms with Redis down")
        check("still returns results", len(outcome.results) > 0)
        check(
            "no single-flight stall (regression: this once took ~13s)",
            degraded.elapsed_ms < 8000,
            f"{degraded.elapsed_ms:.0f}ms",
        )

        spend_report()
        return summary()
    finally:
        keys = [k async for k in raw.scan_iter(match="wss:verify:*")]
        if keys:
            await raw.delete(*keys)
        await raw.aclose()
        await client.aclose()


preflight(need_search_key=True, need_redis=True)
spend_notice(6)
sys.exit(asyncio.run(main()))
