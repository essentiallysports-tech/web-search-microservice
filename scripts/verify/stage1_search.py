"""Stage 1 — search layer against the live Serper API.

Exit criterion: a real query returns ranked, normalized, deduplicated results, and
freshness works natively.

Freshness is the interesting one. It is a pure passthrough of Google's `tbs` codes, so
it either works or the mapping regressed — and a freshness query that silently returns
nothing falls through to the ~5x dearer fallback.

Requires: SERPER_API_KEY. Costs credits — see the notice printed at start.
"""

from __future__ import annotations

import asyncio
import sys

from _harness import (
    Timer,
    check,
    info,
    preflight,
    section,
    spend_notice,
    spend_report,
    summary,
)

from app.config import Settings
from app.models import Freshness
from app.search.brave import BraveProvider
from app.search.serper import MAX_FREE_DEPTH, SerperProvider
from app.services.search_service import SearchService

# Reads .env so the real keys are picked up; caching off so every check below
# actually reaches the provider rather than a warm entry.
settings = Settings(SEARCH_TIMEOUT_S=20.0, CACHE_ENABLED=False)

QUERY = "what is a cdn"


async def main() -> int:
    from app.http_client import build_client, set_client

    client = build_client(settings)
    set_client(client)

    try:
        provider = SerperProvider(settings)
        check("serper configured", provider.enabled, "SERPER_API_KEY present")

        section("a real query returns real results")
        with Timer() as t:
            results = await provider.search(QUERY, count=10)
        info(f"{len(results)} results in {t.elapsed_ms:.0f}ms")
        for r in results[:5]:
            info(f"{r.url[:78]}")

        check("returned results", len(results) > 0, f"{len(results)} results")
        check(
            "at least 5 results for a common query",
            len(results) >= 5,
            f"{len(results)}",
        )

        section("normalization")
        check(
            "all urls are http(s)",
            all(r.url.startswith(("http://", "https://")) for r in results),
        )
        check("urls are unique", len({r.url for r in results}) == len(results))
        check("titles present", all(r.title for r in results))
        check(
            "snippets present",
            sum(1 for r in results if r.snippet) >= len(results) - 1,
            "allowing one snippet-less result",
        )
        check(
            "engine labelled google",
            all(r.engine == "google" for r in results),
            "provenance is the index, not the reseller",
        )

        section("freshness works natively")
        # The check SearXNG could never pass from a blocked IP.
        fresh = await provider.search("openai announcement", count=10, freshness=Freshness.WEEK)
        info(f"{len(fresh)} results for a past-week query")
        check(
            "freshness-constrained query returns results",
            len(fresh) > 0,
            "was always 0 under SearXNG — only the CAPTCHA'd engine supported time_range",
        )
        dated = [r for r in fresh if r.published_at]
        info(f"{len(dated)}/{len(fresh)} carry a date")

        section("result depth and credit cost")
        info(f"one credit covers a depth of {MAX_FREE_DEPTH}; above that Serper bills two")
        check(
            "default result count stays inside one credit",
            settings.default_result_count <= MAX_FREE_DEPTH,
            f"DEFAULT_RESULT_COUNT={settings.default_result_count}",
        )

        section("latency")
        with Timer() as t:
            await service_of().search(f"latency probe {id(object())}", count=10)
        info(f"cold search: {t.elapsed_ms:.0f}ms")
        check(
            "under 5s",
            t.elapsed_ms < 5000,
            f"{t.elapsed_ms:.0f}ms  (Serper advertises 1-2s)",
        )

        section("chain composition")
        service = service_of()
        names = [str(p.name) for p in service.providers]
        info(f"chain: {' -> '.join(names)}")
        check("serper is primary", names[0] == "serper", str(names))
        if settings.brave_api_key:
            check("brave is the fallback", names[1:] == ["brave"], str(names))
            info("Brave only fires on error or under-return; see stage 5.")
        else:
            info("BRAVE_API_KEY unset — running with no fallback index.")

        section("health")
        report = await service.health()
        check("serper healthy", report.providers.get("serper") == "ok", str(report.providers))

        spend_report()
        return summary()
    finally:
        await client.aclose()


def service_of() -> SearchService:
    providers = [SerperProvider(settings), BraveProvider(settings)]
    return SearchService(settings, [p for p in providers if p.enabled], None)


preflight(need_search_key=True)
spend_notice(4)
sys.exit(asyncio.run(main()))
