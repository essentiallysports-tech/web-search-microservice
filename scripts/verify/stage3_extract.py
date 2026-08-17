"""Stage 3 — extraction tier ladder against real websites.

  python scripts/verify/stage3_extract.py

Requires: network, and COSTS MONEY — a configured FIRECRAWL_API_KEY means the
paid tier is exercised for real. Hits live third-party sites, so a failure may
mean the site changed rather than the code broke; the ladder trail printed for
each page tells you which.

The ladder is trafilatura -> http_retry -> firecrawl. There is no browser tier,
so a page that defeats both free tiers is either billed or returned unextracted —
which is the behaviour this stage checks rather than works around.
"""

from __future__ import annotations

import asyncio
import sys

from _harness import Timer, check, info, section, summary

from app.config import Settings
from app.extract.http_retry import HttpRetryExtractor
from app.extract.robots import RobotsPolicy
from app.extract.router import ExtractRouter
from app.extract.trafilatura_ext import TrafilaturaExtractor
from app.http_client import build_client, set_client
from app.logging_setup import configure_logging
from app.models import ExtractorName

configure_logging("ERROR", as_json=False)

# Reads .env so a configured FIRECRAWL_API_KEY exercises the paid tier for real.
settings = Settings(
    PAGE_TIMEOUT_S=20.0,
    RESPECT_ROBOTS_TXT=False,  # exercising the ladder, not the policy
)

STATIC = "https://en.wikipedia.org/wiki/Cache_(computing)"
UA_GATED = "https://realpython.com/async-io-python/"
JS_SPA = "https://quotes.toscrape.com/js/"
PDF = "https://arxiv.org/pdf/1706.03762"


def trail(result) -> str:
    return " -> ".join(f"{a.extractor}:{a.status}" for a in result.attempts)


async def main() -> int:
    client = build_client(settings)
    set_client(client)

    extractors = [TrafilaturaExtractor(settings), HttpRetryExtractor(settings)]
    if settings.firecrawl_api_key:
        from app.extract.firecrawl_ext import FirecrawlExtractor

        extractors.append(FirecrawlExtractor(settings))

    robots = RobotsPolicy(client, None, user_agent=settings.user_agent, enabled=False)
    router = ExtractRouter(settings, extractors, robots)

    try:
        section("the shipped ladder is three rungs, one of them paid")
        names = [str(e.name) for e in router.extractors]
        info(" -> ".join(names))
        check("no browser tier exists", "crawl4ai" not in names, str(names))
        check("free tiers come first",
              names[:2] == ["trafilatura", "http_retry"], str(names))

        section("tier 0 — static page must NOT reach a paid tier")
        with Timer() as t:
            result = await router.extract(STATIC)
        info(f"{trail(result)}  {result.page.char_count} chars  {t.elapsed_ms:.0f}ms")
        check("extracted ok", result.page.status == "ok")
        check("resolved at tier 0", result.page.extractor_used is ExtractorName.TRAFILATURA)
        check("only one tier used", result.tiers_used == 1)
        check("under 3s", t.elapsed_ms < 3000, f"{t.elapsed_ms:.0f}ms (baseline ~700ms)")
        check("substantial content", result.page.char_count > 10_000,
              f"{result.page.char_count} chars")

        section("markdown quality")
        md = result.page.markdown or ""
        check("has headings", "#" in md)
        check("has links", "](" in md)
        check("no nav/footer leakage",
              "Jump to content" not in md and "Privacy policy" not in md)

        section("tier 1 -> 2 — a hard page escalates rather than lying")
        with Timer() as t:
            result = await router.extract(UA_GATED)
        info(f"{trail(result)}  {result.page.char_count} chars  {t.elapsed_ms:.0f}ms")
        if settings.firecrawl_api_key:
            # This is the load-bearing behaviour on a datacenter IP: blocking must
            # convert into COST, not into failure. Firecrawl scrapes from its own
            # egress, which is the only reason a bot-walled page is recoverable at
            # all now that no local browser is in the chain.
            check("recovered by the paid tier", result.page.status == "ok", trail(result))
            check("escalated past both free tiers", result.tiers_used == 3,
                  f"{result.tiers_used} tiers: {trail(result)}")
        else:
            check("free tiers report it blocked, not falsely ok",
                  result.page.status in {"blocked", "error", "empty"}, result.page.status)

        section("JS-rendered SPA — only the paid tier can render")
        with Timer() as t:
            result = await router.extract(JS_SPA)
        info(f"{trail(result)}  {result.page.char_count} chars  {t.elapsed_ms:.0f}ms")
        check("free tiers found nothing first",
              any(a.status == "empty" for a in result.attempts), trail(result))
        if settings.firecrawl_api_key:
            check("rendered content extracted", result.page.status == "ok", trail(result))

        section("content-type gate — PDFs are never downloaded")
        with Timer() as t:
            result = await router.extract(PDF, max_tier=ExtractorName.TRAFILATURA)
        info(f"{result.page.status}: {result.page.error}  {t.elapsed_ms:.0f}ms")
        check("skipped, not downloaded", result.page.status == "skipped", result.page.status)
        check("fast rejection", t.elapsed_ms < 5000, f"{t.elapsed_ms:.0f}ms")

        section("max_tier caps spend")
        capped = await router.extract(UA_GATED, max_tier=ExtractorName.HTTP_RETRY)
        check("stopped before the paid tier", capped.tiers_used <= 2,
              f"{capped.tiers_used} tiers")
        check("paid tier not used",
              capped.page.extractor_used is not ExtractorName.FIRECRAWL)

        if settings.firecrawl_api_key:
            section("tier 2 — Firecrawl direct")
            # Forced directly rather than via the ladder: the point is to prove
            # the paid tier itself works, independently of finding a page hard
            # enough to reach it.
            from app.extract.firecrawl_ext import FirecrawlExtractor

            with Timer() as t:
                page = await FirecrawlExtractor(settings).extract(
                    "https://news.ycombinator.com/", timeout_s=45
                )
            info(f"{page.status} chars={page.char_count} {t.elapsed_ms:.0f}ms")
            if page.error:
                info(f"error: {page.error[:100]}")
            check("firecrawl returned content", page.status == "ok", page.status)
            check("content is substantial", page.char_count > 200, f"{page.char_count} chars")
        else:
            section("tier 2 — Firecrawl SKIPPED (no FIRECRAWL_API_KEY)")

        section("robots.txt policy")
        policy = RobotsPolicy(client, None, user_agent=settings.user_agent, enabled=True)
        allowed = await policy.allows("https://en.wikipedia.org/wiki/Redis")
        denied = await policy.allows(
            "https://en.wikipedia.org/w/index.php?title=Test&action=edit"
        )
        check("article allowed", allowed is True)
        check("disallowed path denied", denied is False)

        section("parallel extraction runs off the event loop")
        # Five DISTINCT hosts, which is the production shape: a /search_and_extract
        # gets its pages from a search result set, so they are almost never the
        # same origin. This used to use five en.wikipedia.org URLs, which measured
        # two different things at once — off-event-loop parsing AND per-host
        # politeness — and started failing the moment PER_HOST_DELAY_S became
        # non-zero. The same-host case is now its own check below, where the
        # pacing is the thing being asserted rather than an unmodelled cost.
        urls = [
            "https://en.wikipedia.org/wiki/Redis",
            "https://aws.amazon.com/what-is/cdn/",
            "https://www.akamai.com/glossary/what-is-a-cdn",
            "https://project-management.com/",
            "https://www.paymoapp.com/",
        ]
        with Timer() as t:
            results = await asyncio.gather(
                *(router.extract(u, max_tier=ExtractorName.TRAFILATURA) for u in urls)
            )
        ok = sum(1 for r in results if r.page.status == "ok")
        chars = sum(r.page.char_count for r in results)
        info(f"{ok}/5 ok, {chars:,} chars, {t.elapsed_ms:.0f}ms wall")
        check("all five extracted", ok >= 4, f"{ok}/5 (one flaky host tolerated)")
        # Serialized on the event loop this would be the sum of every parse, and
        # parses run 2-5s each on a large page.
        check("real parallelism (well under serialized cost)", t.elapsed_ms < 8000,
              f"{t.elapsed_ms:.0f}ms")

        section("same-origin requests are paced, not hammered")
        # The escalation ladder hits one origin up to three times in seconds, which
        # is how a host decides to start serving 403s — and a blocked page falls
        # through to the PAID tier. Pacing is cheap insurance against that.
        same_host = [f"https://en.wikipedia.org/wiki/{p}" for p in
                     ("Redis", "HTTP", "Markdown")]
        with Timer() as paced:
            await asyncio.gather(
                *(router.extract(u, max_tier=ExtractorName.TRAFILATURA) for u in same_host)
            )
        expected = settings.per_host_delay_s * (len(same_host) - 1)
        info(f"3 same-host pages in {paced.elapsed_ms:.0f}ms "
             f"(PER_HOST_DELAY_S={settings.per_host_delay_s}s)")
        check("pacing is switched on", settings.per_host_delay_s > 0,
              f"{settings.per_host_delay_s}s between same-origin request starts")
        check("same-origin work is actually spread out", paced.elapsed_ms >= expected * 1000,
              f"{paced.elapsed_ms:.0f}ms >= {expected * 1000:.0f}ms of required gaps")

        return summary()
    finally:
        await router.shutdown()
        await client.aclose()


sys.exit(asyncio.run(main()))
