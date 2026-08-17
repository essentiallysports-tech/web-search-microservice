"""Stage 5 — fallbacks, reliability, rate limiting, and the LLM layer.

  python scripts/verify/stage5_full.py

Brave, Firecrawl and the LLM layer are exercised only when their keys are configured;
otherwise the script asserts they are correctly INERT.

An inert Brave is not a cost guarantee — the primary is paid too. It means there is no
independent-index fallback, which is a resilience gap.

Requires: Redis/Valkey running (dev compose overlay) and SERPER_API_KEY.
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
    CACHE_VERSION="verify5",
    ENVIRONMENT="dev",
    LOG_LEVEL="ERROR",
    AUTH_ENABLED="true",
    SERVICE_API_KEYS="verify-key-1,verify-key-2",
    RATE_LIMIT_ENABLED="true",
    RATE_LIMIT_PER_MINUTE="12",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import build_search_providers, create_app  # noqa: E402

K1 = {"X-API-Key": "verify-key-1"}
K2 = {"X-API-Key": "verify-key-2"}


def main() -> int:
    settings = Settings()

    section("provider chain composition")
    providers = [str(p.name) for p in build_search_providers(settings)]
    info(f"search providers: {providers}")
    check("serper is primary", providers[:1] == ["serper"], str(providers))
    if settings.brave_api_key:
        check("brave is second in the chain", providers == ["serper", "brave"], str(providers))
        info("Brave is ~5x Serper's unit cost, so it must stay second.")
    else:
        check(
            "brave absent without a key",
            providers == ["serper"],
            str(providers),
        )
        info("No fallback index configured — a Serper outage is a full search outage.")

    with TestClient(create_app()) as client:
        section("auth")
        check("missing key rejected", client.post("/search", json={"query": "q"}).status_code == 401)
        check(
            "wrong key rejected",
            client.post("/search", json={"query": "q"}, headers={"X-API-Key": "nope"}).status_code
            == 401,
        )
        ok = client.post("/search", json={"query": "redis caching", "count": 3}, headers=K1)
        check("valid key accepted", ok.status_code == 200, str(ok.status_code))

        section("rate limiting — cost weighted, per key")
        info(f"budget: {settings.rate_limit_per_minute} units/min")
        info("costs: /search=1  /extract=3  /search_and_extract=4  /research=10")

        headers_seen = ok.headers
        check("X-RateLimit-Limit header present", "X-RateLimit-Limit" in headers_seen)
        check("X-RateLimit-Remaining header present", "X-RateLimit-Remaining" in headers_seen)

        throttled_at = None
        for i in range(30):
            r = client.post("/search", json={"query": f"burst {i}", "count": 1}, headers=K1)
            if r.status_code == 429:
                throttled_at = i
                break
        if throttled_at is not None:
            info(f"key 1 throttled after {throttled_at} extra /search calls")
            check("429 carries Retry-After", "Retry-After" in r.headers)
            r2 = client.post("/search", json={"query": "other key", "count": 1}, headers=K2)
            check(
                "a second key still has its own budget",
                r2.status_code != 429,
                f"got {r2.status_code}",
            )
        else:
            check("rate limit engaged", False, "never throttled — is Redis reachable?")

        section("LLM layer gating")
        research = client.post("/research", json={"query": "what is redis", "count": 3}, headers=K2)
        if settings.enable_llm_layer and settings.anthropic_api_key:
            info("LLM layer enabled — exercising a real call")
            body = research.json()
            check("200", research.status_code == 200, str(research.status_code))
            if research.status_code == 200:
                cites = body.get("citations") or []
                sourced = [r for r in body["results"] if r.get("markdown")]
                info(f"model={body.get('model')} sources={len(sourced)} citations={len(cites)}")
                check("answer present", bool(body.get("answer")))
                urls = {r["url"] for r in body["results"]}
                check(
                    "every citation points at a real source (no hallucinated indices)",
                    all(c in urls for c in cites),
                    str(cites),
                )
                # An empty citation list makes the check above pass trivially,
                # so assert grounding separately — but an honest "the sources
                # don't cover this" is CORRECT behaviour, not a failure. The
                # real fault is an answer that makes claims while citing
                # nothing. Measured: when search returns junk (bing has served
                # dictionary pages for "how does oauth2 work"), the model
                # correctly declines to cite, and the fix belongs in the search
                # layer, not here.
                declined = any(
                    p in (body.get("answer") or "").lower()
                    for p in (
                        "do not contain", "does not contain", "don't contain",
                        "not contain information", "cannot answer",
                        "can't answer", "do not provide", "does not provide",
                        "no information about",
                    )
                )
                if sourced:
                    grounded = len(cites) >= 1 or declined
                    # `check` prints the detail on pass as well as failure, so it
                    # has to describe what was actually observed. It previously
                    # hard-coded the failure explanation and printed "no citations"
                    # directly under a line reporting two of them.
                    detail = (
                        f"{len(cites)} citations over {len(sourced)} sources"
                        if cites
                        else (
                            f"{len(sourced)} sources, no citations, but the answer "
                            "declines honestly"
                            if declined
                            else f"{len(sourced)} sources, no citations, no disclaimer "
                            "— the answer asserts uncited claims"
                        )
                    )
                    check("answer is grounded, or honestly declines", grounded, detail)
                    if declined and not cites:
                        info("model declined to cite — sources did not answer the")
                        info("         question. That is correct behaviour; the")
                        info("         weakness is upstream search quality.")
        else:
            check(
                "/research 503s while the layer is off (cannot spend by mistake)",
                research.status_code == 503,
                str(research.status_code),
            )
            info("set ENABLE_LLM_LAYER=true + ANTHROPIC_API_KEY to exercise synthesis")

        section("retry and circuit-breaker configuration")
        info(f"retry_attempts={settings.retry_attempts} backoff={settings.retry_backoff_s}s")
        info(
            f"circuit: {settings.circuit_fail_threshold} failures / "
            f"{settings.circuit_reset_after_s}s reset"
        )
        check("retries are bounded", 1 <= settings.retry_attempts <= 5)

        section("anti-blocking configuration")
        from app.common.useragents import PROFILES, pick_profile

        seen = {pick_profile(rotate=True).user_agent for _ in range(200)}
        info(f"UA pool: {len(PROFILES)} profiles, {len(seen)} distinct over 200 draws")
        check("rotation is active", len(seen) > 1)
        info(f"per-host concurrency: {settings.per_host_concurrency}")
        info(f"proxy: {settings.proxy_url or '(none)'}")

        section("cost meters")
        billable = [
            line
            for line in client.get("/metrics").text.splitlines()
            if line.startswith("wss_external_calls_total{") and 'billable="true"' in line
        ]
        for line in billable:
            info(line)

        def spend_on(provider: str) -> float:
            return sum(
                float(line.rsplit(" ", 1)[1])
                for line in billable
                if f'provider="{provider}"' in line
            )

        # These are written to assert something REAL in both branches. An earlier
        # version short-circuited on `bool(settings.<key>)`, which meant that with a
        # key configured they passed while printing a label claiming the provider was
        # inert — next to a line showing 12 calls. A check that cannot fail is worse
        # than no check, because it reads as evidence.
        serper_calls = spend_on("serper")
        brave_calls = spend_on("brave")

        if settings.brave_api_key:
            # Brave is ~5x Serper. It should fire only when Serper actually fails,
            # which on a healthy run means never.
            check(
                "Brave did not fire while Serper was healthy",
                brave_calls == 0,
                f"{brave_calls:.0f} brave calls vs {serper_calls:.0f} serper "
                f"— each one costs ~5x, check for under-returning queries",
            )
        else:
            check("Brave inert without a key", brave_calls == 0, f"{brave_calls:.0f}")

        if settings.firecrawl_api_key:
            check(
                "paid extraction stayed incidental",
                spend_on("firecrawl") <= 1,
                f"{spend_on('firecrawl'):.0f} firecrawl calls",
            )
        else:
            check(
                "paid extraction inert without a key",
                spend_on("firecrawl") == 0,
                f"{spend_on('firecrawl'):.0f}",
            )

        # The LLM layer reports tokens, not external_calls — reading the latter
        # could never fail, in either direction.
        llm_in, llm_out = _llm_tokens(client)
        if settings.enable_llm_layer and settings.anthropic_api_key:
            check(
                "the /research call actually spent tokens (so it was real)",
                llm_in > 0 and llm_out > 0,
                f"in={llm_in:.0f} out={llm_out:.0f}",
            )
        else:
            check(
                "the LLM layer spent no tokens while disabled",
                llm_in == 0 and llm_out == 0,
                f"in={llm_in:.0f} out={llm_out:.0f}",
            )

        spend_report()

        section("health")
        health = client.get("/health").json()
        info(f"{health['status']}  {health['providers']}")
        check("healthy", health["status"] == "ok", health["status"])

    return summary()


def _llm_tokens(client) -> tuple[float, float]:
    """Input/output token counters, the only honest evidence the LLM layer ran."""
    totals = {"input": 0.0, "output": 0.0}
    for line in client.get("/metrics").text.splitlines():
        if not line.startswith("wss_llm_tokens_total{"):
            continue
        for kind in totals:
            if f'kind="{kind}"' in line:
                totals[kind] += float(line.rsplit(" ", 1)[1])
    return totals["input"], totals["output"]


preflight(need_search_key=True, need_redis=True)
spend_notice(8)
sys.exit(main())
