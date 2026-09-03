"""Environment-driven configuration.

Every knob that affects cost or latency lives here so it can be tuned on a running
deployment. Settings marked COST are the ones that change the bill.

Background for the non-obvious defaults is in PROGRESS.md; HANDOFF.md "Traps" has
the failure modes worth knowing before changing anything here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import ExtractorName
from app.search.domains import DEFAULT_BLOCKED_DOMAINS, parse_domains


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ service
    service_name: str = "web-search-service"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True

    # --------------------------------------------------------------------- auth
    auth_enabled: bool = False
    # A budget of cost units per minute, not a request count: /research costs 10,
    # /search costs 1. See common/ratelimit.py.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    # Raw CSV, read via the `service_api_keys` property. A `list[str]` field would
    # make pydantic-settings JSON-parse it, breaking the documented `key1,key2` form.
    service_api_keys_raw: str = Field("", validation_alias="SERVICE_API_KEYS")

    @property
    def service_api_keys(self) -> frozenset[str]:
        return frozenset(_csv(self.service_api_keys_raw))

    # ------------------------------------------------------------------- search
    # Both providers are paid; there is no free retrieval path. Cache hit rate is
    # therefore the entire cost story for search, and the TTLs below are cost
    # levers rather than latency ones.
    serper_api_key: str = ""  # required — primary provider
    serper_endpoint: str = "https://google.serper.dev/search"
    # Google `gl` country code. Selects which regional index answers, so it moves
    # result quality more than it looks.
    serper_country: str = "us"

    brave_api_key: str = ""  # blank disables the fallback entirely
    brave_endpoint: str = "https://api.search.brave.com/res/v1/web/search"

    # Dedicated flat-rate resellers, not Apify. Chosen deliberately over Apify's
    # actor marketplace: Apify's real production bill ran far past its own
    # advertised per-item price once platform/compute/storage overhead was
    # counted (observed: ~$1,000/mo for existing unrelated usage against a
    # headline of a few dollars per 1k items) — the same trap this file's
    # COST-marked settings elsewhere exist to avoid. Both of these are flat
    # pay-per-call with no subscription and no platform-rental fee, which is
    # the property that actually matters here, not just the sticker price.
    twitterapi_api_key: str = ""  # blank disables Twitter search entirely
    twitterapi_endpoint: str = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    redditapis_api_key: str = ""  # blank disables Reddit search entirely
    redditapis_endpoint: str = "https://api.redditapis.com/api/reddit/search"

    # Hosts dropped from every result set. Raw CSV for the same reason as
    # SERVICE_API_KEYS — a `list[str]` field would make pydantic-settings
    # JSON-parse it, breaking the documented `a.com,b.com` form.
    #
    # Non-empty by default, which is unusual for a filter and deliberate: the
    # shipped list is the set of hosts the extractor provably cannot read
    # (robots-disallowed, or a JS shell that yields page furniture instead of
    # prose), so leaving them in costs a result slot and sometimes a PAID
    # extraction to return nothing usable. See search/domains.py for the
    # measurements. Set to an empty string to disable filtering entirely.
    search_blocked_domains_raw: str = Field(
        ",".join(DEFAULT_BLOCKED_DOMAINS), validation_alias="SEARCH_BLOCKED_DOMAINS"
    )

    @property
    def search_blocked_domains(self) -> frozenset[str]:
        return parse_domains(self.search_blocked_domains_raw)

    # Serper charges one credit up to a depth of 10 and two above it, so 10 is the
    # natural default — asking for fewer costs the same. A test pins this below the
    # boundary. COST
    default_result_count: int = 10
    max_result_count: int = 20
    # Below this many results the primary counts as failed and the fallback runs,
    # which is a SECOND paid call. Kept low so it fires only on a near-empty answer.
    min_acceptable_results: int = 3
    search_timeout_s: float = 8.0

    # --------------------------------------------------------------- extraction
    # Ladder: trafilatura -> http_retry -> firecrawl. Two free HTTP tiers, then a
    # managed scraper that renders from its own egress IP.
    #
    # There is no local-browser tier and no setting to add one. One existed and was
    # deleted: its wins were `blocked` rescues, and beating a bot wall depends on
    # the egress address rather than the renderer, so those wins erode on a
    # datacenter IP — exactly where they would be needed. PROGRESS.md Phase 12.
    firecrawl_api_key: str = ""  # blank disables the paid tier. COST
    firecrawl_endpoint: str = "https://api.firecrawl.dev/v1/scrape"

    page_timeout_s: float = 15.0
    # The paid tier's own budget, deliberately not shared with PAGE_TIMEOUT_S.
    # Deciding it by a `tier >= N` comparison once handed it the browser's 25s,
    # which exceeded the whole batch deadline and made the tier unreachable by
    # construction. Traps #12. Measured full-ladder times: 3.6s / 12.3s / 12.5s.
    firecrawl_timeout_s: float = 15.0
    # Hard cap on one response body; guards workers against HTML bombs.
    max_html_bytes: int = 4 * 1024 * 1024
    # Shorter extractions are treated as failures and escalated.
    min_extract_chars: int = 250

    # How many of a result set get crawled when the caller doesn't say. The most
    # effective cost control on the combined endpoints: search returns
    # DEFAULT_RESULT_COUNT ranked results and only the top K are paid for. Crawling
    # ten costs twice what five does and adds the slowest page to every request.
    #
    # 0 means "extract nothing". None is not allowed — an unset default is what
    # previously caused every result to be extracted. COST
    default_extract_top_k: int = 5

    # Tier ceiling when a caller omits `max_tier`. Safe as a shipped default because
    # the paid tier is gated behind its own key: with no FIRECRAWL_API_KEY the tier
    # does not exist, so this cannot cause accidental spend.
    #
    # `http_retry` is the free-only ceiling and a legitimate configuration — hard
    # pages come back unextracted rather than billed. Measured on pages weighted to
    # defeat the free tiers: http_retry 4/8 extracted for 0 billed, firecrawl 8/8
    # for 4 billed. COST
    default_max_tier: ExtractorName = ExtractorName.FIRECRAWL

    # Budget for a combined endpoint's whole extraction fan-out. Bounds the
    # ENDPOINT's latency, which is a different question from how long one page may
    # take: derived from the per-page timeouts it came to 40s and measured p95 hit
    # 43s, at which point snippets now beat markdown later. Pages that miss it
    # degrade to snippet-only.
    #
    # MUST stay clear of FIRECRAWL_TIMEOUT_S plus that extractor's transport slack,
    # or the paid tier's budget guard rejects every escalation and the ladder
    # silently loses its top rung. Tests pin both ends.
    extract_deadline_s: float = 25.0

    # The same bound for /extract, which has a different shape: up to 20
    # caller-supplied URLs, so four waves at EXTRACT_CONCURRENCY=5 against the
    # pipeline's single wave. Reusing 25s here would time out most of a full batch.
    #
    # This endpoint had no deadline at all, which left it unbounded AND kept the
    # paid-tier guard inert — the router only checks remaining time when a deadline
    # exists.
    extract_batch_deadline_s: float = 60.0

    max_concurrency: int = 10  # process-wide ceiling on outbound page fetches
    extract_concurrency: int = 5  # per-request fan-out ceiling

    respect_robots_txt: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # -------------------------------------------------------------------- cache
    redis_url: str = "redis://redis:6379/0"
    cache_enabled: bool = True
    # Bumping this invalidates every key at once — use when a payload shape changes.
    cache_version: str = "v1"
    # Stopword-strip and token-sort query keys. Lifts hit rate noticeably, at the
    # cost of merging queries that differ only in word order. See cache/keys.py.
    cache_aggressive_query_key: bool = True

    cache_ttl_search: int = 3600  # COST — direct multiplier on the search bill
    cache_ttl_search_fresh: int = 300  # freshness-constrained queries
    # A rescued partial shouldn't be served for an hour; cache it briefly so the
    # next caller gets another shot at a clean answer.
    cache_ttl_search_degraded: int = 120
    cache_ttl_page: int = 86400
    # Negative caching: remember that a URL failed so we don't pay to retry it on
    # every request. Only failures that are properties of the URL are stored — see
    # services/extract_service.py.
    cache_ttl_failure: int = 1800

    # Compress cached payloads above this size. Page markdown is the bulk of Redis
    # memory, and memory is the recurring cost here.
    cache_compress_min_bytes: int = 1024
    cache_compress_level: int = 3

    # In-process LRU in front of Redis; saves a round trip on hot queries.
    local_cache_size: int = 512
    local_cache_ttl_s: int = 60

    # Single-flight: how long one worker may hold a key's lock, and how long the
    # others wait for its result before computing their own.
    singleflight_lock_ttl_s: int = 30
    singleflight_wait_s: float = 10.0

    # --------------------------------------------------------------- reliability
    # Total tries, not extra ones: 1 disables retrying. Only failures marked
    # retryable are retried — a bad API key is never worth a second attempt.
    retry_attempts: int = 2
    retry_backoff_s: float = 0.3
    circuit_fail_threshold: int = 5
    circuit_reset_after_s: float = 60.0

    # ------------------------------------------------------------ anti-blocking
    # A single fixed UA across thousands of requests is an easy crawler fingerprint.
    rotate_user_agents: bool = True

    # Politeness, per origin. Hammering a host is the fastest way to earn the 403
    # that pushes a page onto the paid tier.
    per_host_concurrency: int = 2

    # Minimum gap between request STARTS to the same origin. The ladder hits one
    # origin up to three times within seconds, which is the behaviour the line above
    # warns about. Demonstrated from a residential IP: realpython.com returned
    # 43,698 chars, then a Cloudflare JS challenge on the very next request.
    #
    # Costs an escalated page up to +1.0s and nothing in the common case, where a
    # request's pages are on different origins. Only the free tiers fetch from this
    # host at all — Firecrawl uses its own egress.
    #
    # Shipped as 0.0 for a long time while the comment claimed otherwise; the stage
    # checks now assert the RUNTIME value, because .env can make a default inert.
    per_host_delay_s: float = 0.5

    # Optional upstream proxy for extraction fetches only, e.g.
    # http://user:pass@gateway:8080. Residential providers expose a single rotating
    # endpoint, so one URL is enough. This is the lever that keeps blocked hosts
    # reachable — and it has never been tested against a real proxy.
    proxy_url: str = ""
    # Search is unaffected either way: both providers authenticate our key, so they
    # have no reason to block us and nothing is gained by proxying them.
    proxy_for_extraction: bool = True

    # ------------------------------------------------------------------ budget
    # Real-dollar spend ceiling — see app/common/budget.py for the full reasoning
    # (fails CLOSED, deliberately, unlike everything else in this file). 0 disables
    # a given window.
    #
    # $30/day is HALF of a $50/day total target for "the Threads routine" end to
    # end — the other $20/day is the sibling es-mcp project's own daily cap for
    # its Threads-specific service bucket (MCP_SERVICE_DAILY_BUDGET_USD there).
    # $20 (Athena/S3) + $30 (this service: search, extraction, Twitter, Reddit)
    # = $50/day, enforced by two independent budgets in two different services.
    #
    # This is server-wide, not per-caller, because Threads is currently the only
    # real consumer of this service — there is no second caller to isolate a
    # sub-budget from yet. If that changes, key this off the authenticated caller
    # identity (security.py already resolves one per request) rather than raising
    # this number to cover a second consumer's traffic under the same ceiling.
    budget_daily_usd: float = 30.0
    budget_monthly_usd: float = 700.0  # ~30 * 23 operating days; adjust once real traffic lands

    # ---------------------------------------------------------------- llm layer
    enable_llm_layer: bool = False
    llm_provider: Literal["anthropic", "ollama"] = "anthropic"
    anthropic_api_key: str = ""
    # Blank = the first-party API. Set to an Anthropic-compatible gateway to route
    # through it, e.g. https://ai-gateway.vercel.sh.
    #
    # Deliberately NOT named ANTHROPIC_BASE_URL: the Anthropic SDK and Claude Code
    # both read that name from the process environment, and OS env outranks .env —
    # so a machine with it exported would silently send a gateway key to
    # api.anthropic.com. Observed, not theoretical. Traps #2.
    llm_base_url: str = ""
    # Gateways namespace model IDs by provider and spell versions differently:
    # first-party `claude-haiku-4-5` is `anthropic/claude-haiku-4.5` on Vercel.
    # Note `effort` is NOT supported on Haiku 4.5; depth control here is max_tokens
    # and prompt design.
    llm_model: str = "claude-haiku-4-5"
    llm_max_tokens: int = 2048  # /research summarizes, it doesn't draft documents
    llm_timeout_s: float = 60.0
    # The main LLM cost lever. Extracted pages run 40k+ chars, so ten unclipped
    # sources would be a six-figure-token prompt. COST
    llm_max_source_chars: int = 6000
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # ------------------------------------------------------------------- guards

    @field_validator(
        "serper_api_key",
        "brave_api_key",
        "firecrawl_api_key",
        "anthropic_api_key",
        "service_api_keys_raw",
        mode="before",
    )
    @classmethod
    def _ignore_inline_comment(cls, value: object) -> object:
        """Treat a value that is really a trailing comment as unset.

        `python-dotenv` parses `FIRECRAWL_API_KEY=   # blank disables it` as the
        VALUE `# blank disables it`. For these fields a truthy value enables a paid
        provider or registers a comment string as a valid API key, so a value that
        is obviously a comment is discarded. No real credential starts with '#'.
        Traps #1.
        """
        if isinstance(value, str) and value.lstrip().startswith("#"):
            return ""
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
