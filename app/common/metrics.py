"""Prometheus metrics.

Cache hit rate and provider mix are first-class: they are the two numbers that say
whether the free path is actually free. Every increment of
`external_calls_total{billable="true"}` is money.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

request_duration = Histogram(
    "wss_request_duration_seconds",
    "End-to-end request latency by endpoint.",
    ["endpoint", "status"],
    buckets=_LATENCY_BUCKETS,
)

cache_events = Counter(
    "wss_cache_events_total",
    "Cache outcomes by layer and namespace.",
    ["namespace", "outcome"],  # outcome: hit|miss|bypass|coalesced|error
)

search_provider_calls = Counter(
    "wss_search_provider_calls_total",
    "Search provider invocations and their outcome.",
    ["provider", "outcome"],  # outcome: ok|empty|error|circuit_open
)

search_fallbacks = Counter(
    "wss_search_fallbacks_total",
    "Times a search provider handed off to the next one in the chain.",
    ["from_provider", "to_provider", "reason"],  # reason: error|circuit_open|too_few
)

# The authoritative spend meter for search: the provider's own reported usage, not a
# local call count, so it already includes surcharges we don't control (Serper bills
# two credits above a result depth of 10).
search_credits_used = Counter(
    "wss_search_credits_total",
    "Search credits the provider reported consuming. This is the bill.",
    ["provider"],
)

# Results dropped by SEARCH_BLOCKED_DOMAINS. Two readings, both worth alerting on:
# a flat zero means the block list is inert (a typo in the CSV looks exactly like
# this), and a number approaching the requested count means the filter is eating
# the result set rather than trimming it — the query needs widening, not the list.
search_domains_filtered = Counter(
    "wss_search_domains_filtered_total",
    "Search results dropped because their host is excluded.",
    ["provider"],
)

search_results_returned = Histogram(
    "wss_search_results_returned",
    "How many results a provider actually returned.",
    ["provider"],
    buckets=(0, 1, 2, 3, 5, 8, 10, 15, 20, 30),
)

extract_attempts = Counter(
    "wss_extract_attempts_total",
    "Extraction attempts by tier and outcome.",
    # unavailable = our breaker was open, i.e. we never tried. Not the URL's fault.
    ["extractor", "outcome"],  # ok|empty|blocked|timeout|error|skipped|unavailable
)

# How often a cheap tier failed and forced a dearer one. With three rungs, every
# escalation out of `http_retry` is a candidate for spend, so this and
# `wss_external_calls_total` should track closely — a gap means the paid-tier guards
# are refusing escalations, and the `reason` label says which one.
extract_escalations = Counter(
    "wss_extract_escalations_total",
    "Escalations out of a tier, by the reason it failed.",
    ["from_extractor", "reason"],  # reason: empty|blocked|timeout|error|resolved
)

# WHY the paid tier is being reached, which is a different question from how often:
#
#   prior_status="empty"    needed JavaScript. IP-independent, so this share should
#                           not move when the deployment moves.
#   prior_status="blocked"  a bot wall Firecrawl's own egress got through. The
#                           IP-sensitive half, and the one to watch after moving to a
#                           VPS — a rising share means our address is scored worse,
#                           which converts directly into bill. PROXY_URL is the lever.
#
# `extractor="http_retry"` rescues are the cheapest win: pages that would otherwise
# have been billed.
extract_rescues = Counter(
    "wss_extract_rescues_total",
    "Successful extractions that a cheaper tier had already failed, by prior status.",
    ["extractor", "prior_status"],
)

extract_tiers_used = Histogram(
    "wss_extract_tiers_used",
    "How many tiers a single extraction consumed.",
    buckets=(1, 2, 3),  # the whole ladder is three rungs
)

external_calls = Counter(
    "wss_external_calls_total",
    "Calls that leave the box. Paid providers here are direct spend.",
    ["provider", "billable"],  # billable: "true"|"false"
)

provider_duration = Histogram(
    "wss_provider_duration_seconds",
    "Latency of a single provider call.",
    ["provider"],
    buckets=_LATENCY_BUCKETS,
)

rate_limit_events = Counter(
    "wss_rate_limit_events_total",
    "Rate limit decisions by outcome.",
    ["outcome"],  # allowed|throttled|error
)

retries_total = Counter(
    "wss_retries_total",
    "Retry attempts by operation. A rising rate is upstream instability.",
    ["operation"],
)

circuit_state = Gauge(
    "wss_circuit_state",
    "Circuit breaker state per provider (0=closed, 1=half_open, 2=open).",
    ["provider"],
)

inflight_requests = Gauge(
    "wss_inflight_requests",
    "Requests currently being served.",
    ["endpoint"],
)

llm_tokens = Counter(
    "wss_llm_tokens_total",
    "Tokens consumed by the optional research layer.",
    ["model", "kind"],  # kind: input|output
)
