"""Request/response schemas and the internal types passed between layers.

`SearchResult` and `ExtractedPage` are the shapes every provider normalizes into, so a
provider swap never reaches the API surface.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

# --------------------------------------------------------------------- enums


class Freshness(StrEnum):
    ANY = "any"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class ExtractorName(StrEnum):
    TRAFILATURA = "trafilatura"
    HTTP_RETRY = "http_retry"  # realistic-UA / HTTP2 retry before paying the scraper
    FIRECRAWL = "firecrawl"


class SearchProviderName(StrEnum):
    SERPER = "serper"
    BRAVE = "brave"
    TWITTERAPI = "twitterapi"
    REDDITAPIS = "redditapis"


class CacheState(StrEnum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"
    COALESCED = "coalesced"  # served from another in-flight request's result


# ------------------------------------------------------------------ internal


class SearchResult(BaseModel):
    """One ranked result, normalized across providers."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    url: str
    snippet: str = ""
    engine: str | None = None
    score: float | None = None
    published_at: str | None = None


class ExtractedPage(BaseModel):
    """Outcome of one extraction attempt — success or failure.

    Status meanings, because the router branches on all of them:
      ok           usable content
      empty        fetched, no main content found (needs rendering)
      blocked      anti-bot wall
      timeout      the host did not answer in time
      error        transport failure — DNS, refused, dead host
      skipped      policy says don't: robots.txt, non-HTML. No tier will do better.
      unavailable  *this extractor* is broken (open circuit), so we learned nothing
                   about the URL. Distinct from `skipped` because it must escalate,
                   and distinct from `error` because it is not evidence about the URL.
    """

    model_config = ConfigDict(extra="ignore")

    url: str
    final_url: str | None = None
    title: str | None = None
    markdown: str | None = None
    text: str | None = None
    status: Literal[
        "ok", "empty", "blocked", "timeout", "error", "skipped", "unavailable"
    ] = "ok"
    extractor_used: ExtractorName | None = None
    error: str | None = None
    fetched_at: float | None = None
    char_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ------------------------------------------------------------------ requests


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=500)]
    # None means DEFAULT_RESULT_COUNT. A hardcoded default here would make that setting
    # silently inert, which is what it was before.
    count: Annotated[int, Field(ge=1, le=20)] | None = None
    lang: Annotated[str, Field(max_length=8)] = "en"
    freshness: Freshness = Freshness.ANY
    bypass_cache: bool = False
    # Hosts to drop from the result set, subdomains included.
    #
    # Omitted means SEARCH_BLOCKED_DOMAINS, so the deployment sets the policy
    # rather than every caller remembering to. An explicit empty list means "no
    # filtering at all" — unset and empty stay distinguishable, the same way
    # `extract_top_k` separates them.
    exclude_domains: Annotated[list[str], Field(max_length=50)] | None = None

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v


class SocialSearchRequest(BaseModel):
    """Twitter/Reddit search — a distinct capability from `/search`, not a
    fallback chain member: a caller picks the platform explicitly, there is no
    "try Twitter, fall back to Reddit" the way Serper falls back to Brave for
    the same intent. See app/api/social.py."""

    model_config = ConfigDict(extra="forbid")

    platform: Literal["twitter", "reddit"]
    query: Annotated[str, Field(min_length=1, max_length=500)]
    count: Annotated[int, Field(ge=1, le=100)] | None = None
    freshness: Freshness = Freshness.ANY
    bypass_cache: bool = False

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: Annotated[list[HttpUrl], Field(min_length=1, max_length=20)]
    bypass_cache: bool = False
    # Cap the extraction tier this request is allowed to reach. A caller that
    # cares about latency or spend can stop at the free tiers with
    # `max_tier="http_retry"`.
    #
    # Omitted means DEFAULT_MAX_TIER, which ships as `firecrawl` — safe as a
    # default only because the paid tier drops out of the ladder entirely without
    # FIRECRAWL_API_KEY, so the key is the single place spend is enabled.
    max_tier: ExtractorName | None = None
    timeout_s: Annotated[float, Field(gt=0, le=60)] | None = None


class SearchAndExtractRequest(SearchRequest):
    extract: bool = True
    #: Omitted means DEFAULT_MAX_TIER (`firecrawl`), which can only spend when
    #: FIRECRAWL_API_KEY is set. Send `http_retry` for a free-only extraction.
    max_tier: ExtractorName | None = None
    # Extract only the top-K of `count` results; the rest come back snippet-only with
    # status "not_attempted". The most effective cost control on this endpoint.
    #
    # Omitted means DEFAULT_EXTRACT_TOP_K, NOT "all of them", so the deployment sets the
    # ceiling rather than each caller. Send 0 for search results with no extraction.
    extract_top_k: Annotated[int, Field(ge=0, le=20)] | None = None
    # Budget for the whole fan-out. Pages that miss it degrade to snippet-only rather
    # than holding up the response.
    extract_deadline_s: Annotated[float, Field(gt=0, le=120)] | None = None


class ResearchRequest(SearchAndExtractRequest):
    # Free-form instruction for the LLM pass, e.g. "summarize as bullet points".
    instruction: str | None = None


# ----------------------------------------------------------------- responses


class ResultItem(BaseModel):
    title: str = ""
    url: str
    snippet: str = ""
    markdown: str | None = None
    extractor_used: ExtractorName | None = None
    #: What happened to THIS result's extraction. Carries every `ExtractedPage`
    #: status, plus:
    #:
    #:   not_attempted  ranked below `extract_top_k`, so no extraction was tried.
    #:                  Distinct from `ok`-with-no-markdown, which would claim we
    #:                  extracted the page and found nothing.
    #:
    #: On `/search` there is no extraction to report and this stays "ok".
    status: str = "ok"
    from_cache: bool = False
    #: When the source says it published, as ISO-8601 UTC — or None when it said
    #: nothing usable. Providers report age in prose ("15 hours ago"), so this is
    #: normalized at the provider boundary; see `app/search/dates.py`.
    #:
    #: Carried through because recency gating is a caller concern this service is
    #: uniquely placed to answer: the date arrives with the search result and is
    #: unrecoverable afterwards without re-fetching every page. Always None on
    #: `/extract`, which has no search result behind it.
    published_at: str | None = None
    #: Provider-specific engagement/relevance signal — Reddit's post score, a
    #: tweet's like count when the provider reports one, None for plain web
    #: search where it isn't meaningful. Added specifically so a caller (the
    #: Threads pipeline) can enforce its own real-reach floor on social results
    #: without a second round trip: dropping this silently at the API boundary
    #: was the actual gap found wiring /social_search into that pipeline — the
    #: number existed on `SearchResult` internally but never reached the HTTP
    #: response. None means "not reported," not "zero" — a caller enforcing a
    #: minimum should treat None as failing that floor, never as passing it.
    score: float | None = None


class SearchResponse(BaseModel):
    query: str
    results: list[ResultItem]
    provider: SearchProviderName | None = None
    cache: CacheState = CacheState.MISS
    took_ms: int = 0
    request_id: str | None = None


class SearchAndExtractResponse(SearchResponse):
    #: How many results carry markdown, and how many were attempted — extraction
    #: quality without scanning the list.
    extracted: int = 0
    attempted: int = 0


class ExtractResponse(BaseModel):
    results: list[ResultItem]
    took_ms: int = 0
    request_id: str | None = None


class ResearchResponse(SearchResponse):
    answer: str | None = None
    citations: list[str] = Field(default_factory=list)
    model: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    providers: dict[str, str]
    version: str
    #: Spend so far this window per window label ("daily"/"monthly"), in
    #: dollars, against each cap — the same number `check()`/`charge()`
    #: enforce, surfaced so a human can see it without reading Prometheus.
    #: Empty when the budget has no configured caps.
    budget: dict[str, dict[str, float]] = Field(default_factory=dict)


# -------------------------------------------------------------- admin: tokens


class TokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which app this token is for. Shown in the admin UI and used to work out who to
    #: talk to before revoking something.
    name: Annotated[str, Field(min_length=1, max_length=120)]
    #: None means no expiry. A year is the practical ceiling — beyond that, rotation
    #: matters more than convenience.
    expires_in_days: Annotated[int, Field(ge=1, le=365)] | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class TokenInfo(BaseModel):
    """A token's metadata. Never includes the secret."""

    id: str
    name: str
    created_at: float
    expires_at: float | None = None
    last_used_at: float | None = None


class TokenCreatedResponse(TokenInfo):
    #: The ONLY time the secret is ever returned. It is stored as a hash, so it cannot
    #: be recovered later — if it is lost, revoke and issue a new one.
    secret: str


class TokenListResponse(BaseModel):
    tokens: list[TokenInfo]
    count: int = 0
    #: How many static SERVICE_API_KEYS are configured. Those are not listed
    #: individually (they have no metadata) but their existence is worth surfacing, so
    #: an empty token list doesn't read as "nothing can authenticate".
    static_keys: int = 0
