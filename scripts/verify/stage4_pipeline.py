"""Stage 4 — the full HTTP surface end to end.

  python scripts/verify/stage4_pipeline.py

Drives the real FastAPI app (via TestClient) against the live search API, Valkey,
and real websites. This is the closest thing to "is the deployed service correct".

Requires: Redis/Valkey running (dev compose overlay), SERPER_API_KEY, and network.
"""

from __future__ import annotations

import os
import sys

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

os.environ.update(
    REDIS_URL=LOCAL_REDIS,
    CACHE_ENABLED="true",
    CACHE_VERSION="verify4",
    ENVIRONMENT="dev",
    LOG_LEVEL="ERROR",
    RESPECT_ROBOTS_TXT="true",
    EXTRACT_CONCURRENCY="5",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

STATIC_URLS = [
    "https://en.wikipedia.org/wiki/Cache_(computing)",
    "https://en.wikipedia.org/wiki/Redis",
    "https://en.wikipedia.org/wiki/HTTP",
]


def main() -> int:
    with TestClient(create_app()) as client:
        section("/health")
        health = client.get("/health")
        body = health.json()
        info(f"{health.status_code} {body['status']} {body['providers']}")
        check("healthy", body["status"] == "ok", body["status"])
        check("serper up", body["providers"].get("serper") == "ok")
        check("cache up", body["providers"].get("cache") == "ok")
        check("trafilatura registered", body["providers"].get("trafilatura") == "ok")
        from app.config import Settings

        cfg = Settings()
        if cfg.firecrawl_api_key:
            check(
                "firecrawl registered (key configured)",
                "firecrawl" in body["providers"],
                str(body["providers"]),
            )
        else:
            check(
                "firecrawl absent without a key (must not cost money by default)",
                "firecrawl" not in body["providers"],
                str(body["providers"]),
            )

        section("POST /search")
        r = client.post("/search", json={"query": "rust ownership explained", "count": 5})
        payload = r.json()
        check("200", r.status_code == 200, str(r.status_code))
        check("results returned", len(payload["results"]) > 0)
        check("no markdown on /search (cheapest endpoint stays cheap)",
              all(i["markdown"] is None for i in payload["results"]))
        check("no extractor ran", all(i["extractor_used"] is None for i in payload["results"]))

        section("/search caching")
        warm = client.post("/search", json={"query": "rust ownership explained", "count": 5}).json()
        check("second call is a hit", warm["cache"] == "hit", warm["cache"])
        check("from_cache flag set", all(i["from_cache"] for i in warm["results"]))

        section("POST /extract — batch")
        with Timer() as cold:
            r = client.post("/extract", json={"urls": STATIC_URLS, "max_tier": "trafilatura"})
        payload = r.json()
        for item in payload["results"]:
            info(f"{item['status']:8} {len(item['markdown'] or ''):7} chars via {item['extractor_used']}")
        check("200", r.status_code == 200)
        check("all extracted", all(i["status"] == "ok" for i in payload["results"]))
        info(f"cold batch: {cold.elapsed_ms:.0f}ms")

        with Timer() as warm_t:
            payload = client.post(
                "/extract", json={"urls": STATIC_URLS, "max_tier": "trafilatura"}
            ).json()
        check("warm batch all cached", all(i["from_cache"] for i in payload["results"]))
        info(f"warm batch: {warm_t.elapsed_ms:.0f}ms "
             f"({cold.elapsed_ms / max(warm_t.elapsed_ms, 0.01):.0f}x)")

        section("/extract — order and duplicates")
        dup = [STATIC_URLS[0], STATIC_URLS[1], STATIC_URLS[0]]
        payload = client.post("/extract", json={"urls": dup, "max_tier": "trafilatura"}).json()
        check("all entries returned", len(payload["results"]) == 3)
        check("input order preserved", [i["url"] for i in payload["results"]] == dup)

        section("/extract — negative cache")
        bad = "https://this-domain-does-not-exist-9f8a7b6c.example/article"
        with Timer() as first:
            a = client.post("/extract", json={"urls": [bad]}).json()["results"][0]
        with Timer() as second:
            b = client.post("/extract", json={"urls": [bad]}).json()["results"][0]
        info(f"1st {first.elapsed_ms:.0f}ms -> 2nd {second.elapsed_ms:.0f}ms")
        check("genuinely failed", a["status"] != "ok", a["status"])
        check("repeat served from the negative cache", b["from_cache"] is True)
        check("repeat is much faster", second.elapsed_ms < first.elapsed_ms)

        section("/extract — robots.txt honoured")
        payload = client.post(
            "/extract",
            json={"urls": ["https://en.wikipedia.org/w/index.php?title=X&action=edit"]},
        ).json()
        check("disallowed URL skipped", payload["results"][0]["status"] == "skipped")

        section("POST /search_and_extract")
        with Timer() as t:
            r = client.post(
                "/search_and_extract",
                json={"query": "what is redis used for", "count": 5, "extract_top_k": 3},
            )
        payload = r.json()
        info(f"{r.status_code} {len(payload['results'])} results, "
             f"{payload['extracted']}/{payload['attempted']} extracted, {t.elapsed_ms:.0f}ms")
        check("200", r.status_code == 200, str(r.status_code))
        check("results returned", len(payload["results"]) > 0)
        check("only top-k extracted",
              sum(1 for i in payload["results"] if i["markdown"]) <= 3)
        check("non-extracted results still carry snippets",
              all(i["snippet"] or i["markdown"] for i in payload["results"]))
        check("at least one extraction succeeded", payload["extracted"] >= 1,
              f"{payload['extracted']} extracted")

        section("/search_and_extract — extract=false is snippet-only")
        payload = client.post(
            "/search_and_extract",
            json={"query": "what is redis used for", "count": 5, "extract": False},
        ).json()
        check("no markdown", all(i["markdown"] is None for i in payload["results"]))
        check("nothing attempted", payload["attempted"] == 0)

        section("cost metrics")
        for line in client.get("/metrics").text.splitlines():
            if line.startswith((
                "wss_cache_events_total{",
                "wss_extract_attempts_total{",
                "wss_external_calls_total{",
            )):
                info(line)

        # "No billable calls" is no longer a meaningful invariant: search itself is
        # paid, so a working run MUST spend. The invariants worth holding are that
        # search spend tracks cache misses rather than request count, and that paid
        # *extraction* stays at zero on ordinary pages.
        section("cost invariants")
        billable = [
            line
            for line in client.get("/metrics").text.splitlines()
            if line.startswith("wss_external_calls_total{") and 'billable="true"' in line
        ]

        def spend_on(provider: str) -> float:
            return sum(
                float(line.rsplit(" ", 1)[1])
                for line in billable
                if f'provider="{provider}"' in line
            )

        search_calls = spend_on("serper") + spend_on("brave")
        info(f"search provider calls: {search_calls:.0f}")
        for line in billable:
            info(line)

        # Four search-bearing requests were made above over two DISTINCT queries
        # (each issued twice). Anything above two means the cache is not doing its
        # job — and with a paid primary that is money, not just latency.
        #
        # Zero is also correct: a re-run inside CACHE_TTL_SEARCH finds both queries
        # already warm in Valkey, which is the same property being asserted.
        check(
            "search spend tracks distinct queries, not request count",
            search_calls <= 2,
            f"{search_calls:.0f} calls for 4 requests over 2 distinct queries",
        )

        check(
            "paid extraction tier never fired on ordinary pages",
            spend_on("firecrawl") == 0,
            f"{spend_on('firecrawl'):.0f} firecrawl calls",
        )

        if cfg.brave_api_key:
            # Brave firing at all means Serper errored or under-returned. Not a
            # failure, but it is 5x the unit cost and worth seeing.
            brave = spend_on("brave")
            if brave:
                info(f"NOTE: Brave fallback fired {brave:.0f}x — check Serper's health")
            else:
                info("Brave never fired; Serper answered everything")

        spend_report()

    return summary()


preflight(need_search_key=True, need_redis=True)
spend_notice(4)
sys.exit(main())
