"""Benchmarks — latency, cache economics, and the cost model.

  python scripts/verify/benchmark.py
  python scripts/verify/benchmark.py --compare-tavily    # needs TAVILY_API_KEY

Answers the question the whole project exists to answer: is this cheaper and fast
enough than a managed tool? Prints latency percentiles, cache hit rate, tier
distribution, and a projected cost per 1,000 requests.

The shape of that answer changed when SearXNG was dropped. There is no longer a
$0-marginal path to point at: search costs ~$1/1k and the saving now comes from
three places instead — a cheaper per-query rate than the managed tools, a cache
that removes most queries from the bill entirely, and extraction that stays free
for most pages. Section 7 prices all three.

Requires: Redis/Valkey running, SERPER_API_KEY, and network. This script makes
~80 billable search calls.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

from _harness import (
    LOCAL_REDIS,
    USD_PER_CREDIT,
    env_or_dotenv,
    info,
    preflight,
    section,
    spend_notice,
)

os.environ.update(
    REDIS_URL=LOCAL_REDIS,
    CACHE_ENABLED="true",
    CACHE_VERSION="bench",
    ENVIRONMENT="dev",
    LOG_LEVEL="ERROR",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

# A fixed set so runs are comparable over time. Mixed intent: informational,
# commercial, technical, and news-shaped.
QUERIES = [
    "what is a cdn",
    "best project management software 2026",
    "python asyncio vs threading",
    "redis vs memcached performance",
    "how does oauth2 work",
    "kubernetes ingress controller comparison",
    "rust borrow checker explained",
    "postgres index types",
    "latest ai regulation news",
    "typescript generics tutorial",
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[index]


def report(label: str, samples: list[float]) -> None:
    if not samples:
        info(f"{label}: no samples")
        return
    info(
        f"{label:32} n={len(samples):3}  "
        f"p50={statistics.median(samples):7.0f}ms  "
        f"p95={pct(samples, 0.95):7.0f}ms  "
        f"max={max(samples):7.0f}ms"
    )


def timed(client, path: str, payload: dict) -> tuple[float, dict]:
    started = time.perf_counter()
    response = client.post(path, json=payload)
    elapsed = (time.perf_counter() - started) * 1000
    return elapsed, response.json()


def main() -> int:
    with TestClient(create_app()) as client:
        section("1. /search — cold (cache bypassed each time)")
        cold = []
        for query in QUERIES:
            ms, _ = timed(client, "/search", {"query": query, "count": 5, "bypass_cache": True})
            cold.append(ms)
        report("/search cold", cold)

        section("2. /search — warm (all cached)")
        warm = []
        for query in QUERIES:
            ms, body = timed(client, "/search", {"query": query, "count": 5})
            warm.append(ms)
            assert body["cache"] == "hit", f"expected a hit for {query!r}"
        report("/search warm", warm)
        info(f"speedup: {statistics.median(cold) / max(statistics.median(warm), 0.01):.0f}x")

        section("3. /search_and_extract — cold, top-3 extracted")
        combined_cold = []
        extracted_counts = []
        for query in QUERIES[:6]:
            ms, body = timed(
                client,
                "/search_and_extract",
                {"query": query, "count": 5, "extract_top_k": 3, "bypass_cache": True},
            )
            combined_cold.append(ms)
            extracted_counts.append(body["extracted"])
        report("/search_and_extract cold", combined_cold)
        info(f"extraction success: {sum(extracted_counts)}/{len(extracted_counts) * 3} pages")

        section("4. /search_and_extract — warm")
        combined_warm = []
        for query in QUERIES[:6]:
            ms, _ = timed(
                client,
                "/search_and_extract",
                {"query": query, "count": 5, "extract_top_k": 3},
            )
            combined_warm.append(ms)
        report("/search_and_extract warm", combined_warm)

        section("5. cache economics under a realistic repeat pattern")
        # Fresh queries, not QUERIES — the sections above already warmed those,
        # which made this report a meaningless 100% hit rate.
        # Real traffic is heavily skewed: a few queries dominate. This models a
        # long tail with an 80/20 split.
        fresh = [f"{q} {int(time.time())}" for q in QUERIES]
        traffic = [fresh[0]] * 20 + [fresh[1]] * 12 + fresh[2:] * 2
        before = _counter_snapshot(client, "wss_search_provider_calls_total")
        hits = 0
        for query in traffic:
            _, body = timed(client, "/search", {"query": query, "count": 5})
            hits += body["cache"] in ("hit", "coalesced")
        after = _counter_snapshot(client, "wss_search_provider_calls_total")

        upstream = after - before
        info(f"requests: {len(traffic)}   distinct queries: {len(set(traffic))}")
        info(f"cache hits: {hits}   hit rate: {hits / len(traffic) * 100:.0f}%")
        info(f"upstream provider calls: {upstream:.0f}")
        info(f"calls avoided: {len(traffic) - upstream:.0f}")
        info(f"cost reduction vs no cache: {(1 - upstream / len(traffic)) * 100:.0f}%")

        section("6. extraction tier distribution")
        tiers = _label_counts(client, "wss_extract_attempts_total")
        total = sum(tiers.values()) or 1
        for name, value in sorted(tiers.items(), key=lambda kv: -kv[1]):
            info(f"{name:34} {value:6.0f}  ({value / total * 100:4.1f}%)")

        section("7. projected cost per 1,000 /search_and_extract requests")
        billable = _label_counts(client, "wss_external_calls_total", only='billable="true"')
        billable_total = sum(billable.values())
        credits = _counter_snapshot(client, "wss_search_credits_total")
        info(f"billable calls this run: {billable_total:.0f}")
        info(f"search credits consumed: {credits:.0f}")
        info("")

        # The hit rate measured in section 5 is what actually sets the bill, so
        # project from it rather than from list price alone.
        hit_rate = hits / len(traffic)
        paid_per_1k = 1000 * (1 - hit_rate)
        projected = paid_per_1k * USD_PER_CREDIT
        info(f"measured cache hit rate:      {hit_rate * 100:.0f}%")
        info(f"paid searches per 1k requests: {paid_per_1k:.0f}")
        info(f"projected search cost / 1k:    ${projected:.2f}")
        info("")
        info("Against managed pricing (approximate list prices):")
        info(f"  this service      ~${projected:.2f} per 1k   (Serper + {hit_rate * 100:.0f}% cache)")
        info("  Serper, uncached  ~$1.00 per 1k   (~$0.30 at volume)")
        info("  Brave API         ~$5.00 per 1k   (fallback only)")
        info("  Tavily            ~$8.00 per 1k")
        info("  Firecrawl         per-page        (tier 3 only)")
        info("")
        info("Two numbers decide the real bill, and both are exported:")
        info("  wss_cache_events_total     — every hit is a search you did not buy")
        info("  wss_search_credits_total   — the vendor's own figure, so it counts the")
        info("                               double charge on result depths above 10")
        info("")
        info("Extraction is where the free path still exists: tier 0+1 pages cost")
        info("nothing per page. Section 6's distribution is that story.")

        if "--compare-tavily" in sys.argv:
            _compare_tavily(client)

    return 0


def _counter_snapshot(client, metric: str) -> float:
    total = 0.0
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(metric + "{") and not line.startswith(metric + "_created"):
            total += float(line.rsplit(" ", 1)[1])
    return total


def _label_counts(client, metric: str, only: str | None = None) -> dict[str, float]:
    counts: dict[str, float] = {}
    for line in client.get("/metrics").text.splitlines():
        if not line.startswith(metric + "{"):
            continue
        if only and only not in line:
            continue
        labels, value = line.rsplit(" ", 1)
        counts[labels[len(metric) + 1 : -1]] = float(value)
    return counts


def _compare_tavily(client) -> None:
    """Side-by-side against Tavily on the same queries."""
    import httpx

    key = env_or_dotenv("TAVILY_API_KEY")
    if not key:
        section("8. Tavily comparison — SKIPPED")
        info("set TAVILY_API_KEY in .env or the environment to enable")
        return

    section("8. Tavily comparison (same queries, both cold)")
    ours: list[float] = []
    theirs: list[float] = []
    our_chars: list[int] = []
    their_chars: list[int] = []

    for query in QUERIES[:5]:
        ms, body = timed(
            client,
            "/search_and_extract",
            {"query": query, "count": 5, "extract_top_k": 3, "bypass_cache": True},
        )
        ours.append(ms)
        our_chars.append(sum(len(i["markdown"] or "") for i in body["results"]))

        started = time.perf_counter()
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "max_results": 5,
                "include_raw_content": True,
            },
            timeout=60,
        )
        theirs.append((time.perf_counter() - started) * 1000)
        payload = response.json()
        their_chars.append(
            sum(len(r.get("raw_content") or "") for r in payload.get("results", []))
        )

    report("ours  (cold, top-3 extracted)", ours)
    report("tavily", theirs)
    info(f"content volume — ours: {sum(our_chars):,} chars, tavily: {sum(their_chars):,} chars")
    info(f"tavily cost for this run: ~${len(QUERIES[:5]) * 0.008:.3f}; ours: $0 marginal")
    info("")
    info("Read this honestly: we extract the top 3, Tavily returns raw content for")
    info("all 5, so the content-volume gap is partly a depth difference, not purely")
    info("quality. On latency Tavily wins outright and will keep winning — it runs a")
    info("dedicated extraction fleet, while this service renders pages on one box.")
    info("The trade being made is latency for marginal cost, and warm-cache reads")
    info("(~5ms) are where this service beats it.")


preflight(need_search_key=True, need_redis=True)
spend_notice(80)
sys.exit(main())
