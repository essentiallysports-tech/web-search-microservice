# Implementation Progress

Running record of what has been built, why it was built that way, and what was
actually verified. Companion to `web-search-microservice-plan.md` (the original
plan) and `README.md` (how to use the result).

**Rules for this document**

- One section per phase, appended as the phase completes.
- Every "Verified" claim is something that was actually run, with its result.
  Anything not exercised is listed under "Not yet verified".
- Deviations from the original plan are recorded with the reason, not silently
  applied.

---

## Status

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Repo scaffolding & provider interfaces | ✅ Complete |
| 1 | Search layer (SearXNG primary) | ✅ Complete |
| 2 | Caching & single-flight | ✅ Complete |
| 3 | Extraction layer (4-tier router) | ✅ Complete |
| 4 | Combined pipeline (`/search_and_extract`) | ✅ Complete |
| 5 | Fallbacks (Brave + Firecrawl) | ✅ Complete |
| 6 | Reliability & anti-blocking | ✅ Complete |
| 7 | Optional LLM layer (`/research`) | ✅ Complete |
| 8 | Productionizing | ✅ Complete |
| 9 | Re-platform onto a paid search provider | ✅ Code complete, ⚠️ unverified live |

**All phases implemented and verified end to end** — unit suite, five live stage
scripts, benchmarks against Tavily, and a containerized run including horizontal
scaling. See "Verification sweep" below and `TESTING.md` for how to re-run it.

**Test suite: 371 passing.**

## Decisions locked at kickoff

| Question | Answer | Consequence |
|----------|--------|-------------|
| Ops model | Self-hosted first | SearXNG primary, Brave as fallback |
| Volume | 10k–200k queries/month | 4 vCPU / 8GB target; horizontal seams built in from the start |
| Extraction | Full stack | 4 tiers incl. Firecrawl |
| LLM layer | Cheap API tier | Claude Haiku 4.5, strictly opt-in |

Phase order was changed from the original plan: **caching moved from Phase 4 to
Phase 2**, because it shapes the service interfaces and retrofitting it costs
more than building around it.

---

## Phase 0 — Repo scaffolding & provider interfaces ✅

### Delivered

| File | Role |
|------|------|
| `app/main.py` | App factory, lifespan, request-id + metrics middleware, ops endpoints |
| `app/config.py` | ~50 env-driven knobs; fails fast on bad prod config |
| `app/models.py` | Request/response schemas + internal `SearchResult` / `ExtractedPage` contracts |
| `app/http_client.py` | One shared HTTP/2 client per process |
| `app/security.py` | API-key auth, constant-time comparison |
| `app/cache/keys.py` | Query + URL normalization |
| `app/common/circuit.py` | Per-provider circuit breaker |
| `app/common/metrics.py` | Prometheus: cache events, provider mix, billable-call counter |
| `app/search/base.py`, `app/extract/base.py`, `app/rerank/base.py` | Provider interfaces |
| `Dockerfile` | Two-stage, with `EXTRAS` / `INSTALL_BROWSER` build args |
| `docker-compose.yml` | api + searxng + redis |
| `searxng/settings.yml` | JSON API on, limiter off, curated engines |

### Key decisions

**Valkey instead of Redis.** BSD-licensed drop-in; `redis-py` talks to it
unchanged. One-line swap back to `redis:7-alpine` if preferred.

**Cache keys fold `http`/`https` and drop `www.`.** Same document on essentially
every site, so it is free hit-rate. Applied to keys only — fetches always use the
URL exactly as the engine returned it.

**Aggressive query keys are on by default.** Stopword-stripped and token-sorted,
which merges `best CRM 2026` / `Best CRM 2026?` / `the best CRM of 2026` onto one
key. Known tradeoff: it also merges word-order variants (`go vs rust` ≡
`rust vs go`). Disable with `CACHE_AGGRESSIVE_QUERY_KEY=false`.

**One uvicorn worker per container.** Keeps Prometheus counters correct without
the multiprocess collector; scaling story is `--scale api=N`.

**Negative caching is a first-class TTL** (`CACHE_TTL_FAILURE`). Re-fetching a
known-bad URL on every request is a pure cost with no upside.

**No `ORJSONResponse`.** Current FastAPI serializes faster natively; orjson is
retained for cache payload encoding.

### Deviations from the plan

- Added `app/api/` for routers. The plan put route wiring in `main.py`; with four
  endpoints coming that gets crowded.
- Added `app/common/` (circuit breaker, metrics) — not in the plan's layout, but
  needed by both the search and extract layers.

### Verified

- 19 cache-key tests pass, covering near-duplicate merging, tracking-param
  stripping, param ordering, and version-bump invalidation.
- App boots; `/livez`, `/health`, `/metrics` all return 200 with request-id
  headers; structured JSON logs emit correctly.
- Schema validation rejects blank queries, out-of-range counts, unknown fields,
  malformed URLs.
- Circuit breaker walks closed → open → half-open → closed, and a failed probe
  correctly re-opens it.

---

## Phase 1 — Search layer (SearXNG primary) ✅

### Delivered

| File | Role |
|------|------|
| `app/search/searxng.py` | `SearXNGProvider` — JSON API client, normalization, failure classification |
| `app/services/search_service.py` | Provider chain, fallback policy, partial-result rescue |
| `app/api/search.py` | `POST /search` |
| `app/api/deps.py` | Route dependencies reading off `app.state` |
| `docker-compose.dev.yml` | Dev overlay exposing SearXNG/Redis on loopback |
| `tests/test_searxng_provider.py` | 30 tests |
| `tests/test_search_service.py` | 18 tests |
| `tests/test_search_endpoint.py` | 15 tests |

### API contract

```
POST /search
{ "query": "best crm 2026", "count": 5, "lang": "en",
  "freshness": "any|day|week|month|year", "bypass_cache": false }

200 → { "query", "results": [{title, url, snippet, markdown: null,
                              extractor_used: null, status, from_cache}],
        "provider": "searxng", "cache", "took_ms", "request_id" }
422 → validation error
502 → all providers failed (body carries per-provider attempt detail)
```

`/search` never extracts and never calls an LLM. Enforced by what the module
imports, not by a runtime flag.

### Key decisions

**Partial-result rescue.** A thin-but-real result set is retained while the chain
continues. If SearXNG returns 2 results (below `MIN_ACCEPTABLE_RESULTS`) and Brave
then fails outright, the caller gets those 2 rather than a 502 — the primary call
was already spent, and discarding it helps nobody.

**Failure classification drives the breaker.** `SearchProviderError` carries a
`retryable` flag: 5xx and timeouts are retryable, 4xx config errors are not.

**Merged-engine attribution is preserved.** A result that several engines agreed
on keeps the full list (`"bing,duckduckgo"`) rather than one name — that
agreement is ranking signal worth keeping.

**`unresponsive_engines` is a Prometheus metric, not a log line.**
`wss_searxng_unresponsive_engines_total{engine,reason}` is the earliest warning
that the free path is degrading — it moves well before result quality visibly
drops or Brave's traffic share climbs.

**Settings come from `app.state`, never `config.get_settings()`, inside routes.**
The latter is an `lru_cache`d process global, so a route bound to it silently
ignores the config the running app actually booted with. This was caught by a
failing test, not by inspection.

### Findings from the live SearXNG instance

These were discovered by running the real image (`searxng/searxng:2026.8.12`),
and several contradict reasonable assumptions:

**1. The image will not boot on the placeholder secret, and does not replace it.**
The entrypoint's `sed` that substitutes `ultrasecretkey` only runs when it
*creates* `settings.yml` from its own template. Mounting our own file means it
never fires, and SearXNG exits with
`server.secret_key is not changed`. Fix: `SEARXNG_SECRET` env var, which
overrides `server.secret_key` directly. `docker-compose.yml` now requires it.

**2. `google` ships `inactive: true` upstream — it is unavailable at any config.**
The plan's premise that SearXNG gives you Google results is not true of current
SearXNG. Brave and DuckDuckGo carry the load.

**3. `keep_only` selects which engines exist; it does not enable them.**
`bing`, `mojeek`, `qwant` ship `disabled: true`. Listing them under `keep_only`
without an explicit `disabled: false` leaves them registered but never queried.
This silently collapsed the result mix to DuckDuckGo alone.

**4. An unknown engine name fails confusingly.** SearXNG treats a `name:` it
doesn't recognize as a *new* engine definition, then errors with
`The "engine" field is missing` rather than "unknown engine". `stackexchange` is
not a valid name — the real one is `stackoverflow`.

**5. Only `general`-category engines run for a default query.** `github` (it),
`arxiv` (science), `openalex` (science) never fire on a normal search. They were
removed rather than left as dead config; they can return when the API exposes
category routing.

**6. A disallowed output format returns 403 + HTML, not 200 + HTML.**
So a 403 on `format=json` most likely means `json` is missing from
`search.formats` — *not* the limiter. The provider's 403 message was rewritten to
lead with the correct cause; leading with the limiter would send an operator down
the wrong path.

**7. Enabling the limiter requires Valkey.** Without it SearXNG exits at startup
rather than degrading, so "limiter misconfigured" presents as a boot failure, not
as 403s.

**8. Upstream blocking is immediate and real.** On the very first query from a
residential IP, Brave returned `too many requests` and Startpage a CAPTCHA. This
is the Phase 0 concern confirmed in practice, and it is why provider mix is
tracked from day one rather than in Phase 8.

### Verified

**Unit / contract — 82 tests passing.**
- Normalization: field mapping, merged-engine attribution, non-HTTP entries
  dropped, canonical-URL dedupe, count slicing, malformed entries tolerated.
- Request params: freshness → `time_range` for all four values, omitted for
  `any`, engine override forwarding, blank lang → `all`.
- Failure modes: 403 message names the formats cause, HTML body detected, 429 and
  5xx marked retryable, 4xx not, timeouts and malformed JSON classified, repeated
  failure opens the breaker.
- Service: fallback on error / open circuit / under-return; partial rescue
  including "last provider's empty answer must not displace a better earlier
  partial"; total failure raises with per-provider detail.
- Endpoint: response shape, `count` clamped by `MAX_RESULT_COUNT`, 422 matrix,
  502 on total failure, auth accept/reject, `/health` 200-vs-503, `/livez` stays
  200 when providers are down.

**Live, against the running container.**
- `GET /search?format=json` → 200, `application/json`, results parse cleanly.
  **No 403.** (The plan's Phase 1 exit criterion.)
- Engine mix before the settings fix: 10 results, **duckduckgo only**.
  After: 38 results across **brave + bing + duckduckgo**, with merged-engine
  consensus visible in the scores.
- All engines register cleanly except `wikidata`, which fails init against a
  transient upstream 403.
- End-to-end through `SearchService`: 0.8–1.5s per query, correct
  `time_range` propagation for freshness, all URLs HTTP(S) and unique.
- `/health` reports `{"searxng": "ok"}`.

### Not yet verified

- **The Docker image has never been built.** `docker compose up searxng redis`
  works; the `api` service image (which pulls Chromium) has not been built. The
  app has only been run from a local virtualenv against the containerized SearXNG.
- Behaviour when SearXNG is entirely unreachable is unit-tested, not exercised
  against a real stopped container.
- No load or concurrency testing.

### Known limitations

- **Single result page.** `max_page: 1`; a request for `count=20` returns
  whatever one page yields (typically 10–38 merged). Pagination is not
  implemented.
- **No category routing.** Every query runs as `general`, so vertical engines
  cannot contribute. This is why they were dropped from the engine set.
- **No caching yet** — every `/search` is a live provider call. `cache` in the
  response is hardcoded to `bypass` until Phase 2.
- **No retries** — a single transient failure fails the provider and moves to the
  next. Retries with backoff are Phase 6.
- `mojeek` was enabled but has not been observed contributing results; worth
  re-checking once there is more query volume.

---

## Phase 2 — Caching & single-flight ✅

Moved ahead of extraction because it shapes the service interfaces.

### Delivered

| File | Role |
|------|------|
| `app/cache/codec.py` | orjson + zstd with a 1-byte frame header |
| `app/cache/local.py` | L1 in-process TTL+LRU cache |
| `app/cache/redis_cache.py` | L2 wrapper; fails open, circuit-broken |
| `app/cache/singleflight.py` | In-process request coalescing |
| `app/cache/layer.py` | Composes L1 → L2 → coalesced compute |
| `tests/test_cache_primitives.py` | 22 tests |
| `tests/test_cache_layer.py` | 22 tests |
| `tests/test_search_caching.py` | 17 tests |

### The read path

```
get_or_compute(key)
  ├─ L1 process memory ─────────────────► HIT      (~0ms, no network)
  ├─ L2 Redis ──────────────────────────► HIT      (~3ms, repopulates L1)
  └─ miss
       ├─ in-process single-flight ─────► COALESCED  (same worker)
       └─ Redis lock
            ├─ acquired → compute, store, release → MISS
            ├─ held     → poll L2 for leader's result → COALESCED
            └─ unavailable → compute immediately → MISS
```

### Key decisions

**Four layers of duplicate protection, not one.** L1 removes the network hop for
hot keys; L2 shares across replicas; in-process single-flight collapses
concurrent duplicates inside a worker; the Redis lock collapses them across
workers. Each catches a case the others cannot.

**Everything fails open.** A cache that cannot be reached degrades the service to
"uncached but working". Losing the cache is a cost problem, never an
availability one — and `/health` reflects that by reporting a dead cache as
`degraded` while the service stays `ok`.

**Cache status is excluded from the readiness computation.** A healthy cache must
never be able to mask every search provider being down.

**TTL is a function of the value, not just the key.** `_ttl_for` returns
`degraded (120s) < freshness-constrained (300s) < normal (3600s)`. Caching a
rescued partial for a full hour would turn one bad upstream minute into an hour
of bad answers.

**`attempted` is deliberately not persisted.** It describes one request's
provider chain, not the result; caching it would replay a stale failure story to
later callers.

**Bypass still writes.** `bypass_cache` skips *reading*, but the fresh result is
stored — the caller already paid for it, so everyone else should benefit.

**Compression is framed, not guessed.** A one-byte header distinguishes raw from
zstd, so the decoder never has to sniff. Values under
`CACHE_COMPRESS_MIN_BYTES` skip compression entirely.

**A corrupt cache value is a miss, not an error.** It is logged, deleted, and
recomputed.

### The bug the live test found

The unit tests passed and the live functional checks passed — but the timestamps
in the "Redis unreachable" scenario showed a single request taking **~13 seconds**.

`try_lock` returned a plain `False` both when another worker held the lock *and*
when Redis was unreachable. The layer read that as "someone else is computing",
and waited out the entire `SINGLEFLIGHT_WAIT_S` — issuing a Redis GET per poll,
each hitting a 2s socket timeout.

Two fixes:

1. `try_lock` now returns a tri-state `LockOutcome` (`ACQUIRED` / `HELD` /
   `UNAVAILABLE`). There is no leader to wait for when Redis is down, so
   `UNAVAILABLE` computes immediately.
2. `RedisCache` got its own circuit breaker. After `CIRCUIT_FAIL_THRESHOLD`
   failures it stops touching Redis entirely for `CIRCUIT_RESET_AFTER_S`, which
   removes the per-operation socket timeout from every subsequent request.
   `ping()` deliberately bypasses the breaker — it is how recovery is detected.

Result: **~13s and 12 timed-out calls → ~2s and 2 calls**, then zero overhead.
`CircuitBreaker.record_success` also got a lock-free fast path, since it is now
called on every cache read.

### Verified

**Unit — 141 tests passing (59 new).**
- Codec: raw/zstd round-trips, unicode, corrupt frame / bad JSON / broken zstd
  all classified, compression verified to actually shrink prose >3x.
- L1: LRU eviction order, lazy expiry, `None` as a storable value distinct from
  a miss, disabled-when-size-zero.
- Single-flight: 10 concurrent → 1 call; failures propagate to all waiters; key
  released after failure; a cancelled waiter does not kill the leader.
- Layer: L1-before-L2 ordering, L2 hit repopulating L1, bypass semantics,
  callable TTL, failed computes not cached, lock released after a failed
  compute, corrupt value treated as a miss, and the two regression tests for the
  Redis-down latency bug.
- Service: envelope round-trip, `attempted` not persisted, near-duplicate query
  merging, key sensitivity to count/lang/freshness, coalescing, TTL policy.

**Live, against Valkey + SearXNG.**

| Check | Result |
|---|---|
| Cold miss → warm hit | 1158ms → **0ms** |
| L2 hit from a cold process (simulating a second replica) | 3ms |
| Near-duplicate query (`"  Python  TYPE hints guide? "`) | HIT — one upstream call |
| Key sensitivity: count / lang / freshness | all correctly MISS |
| 8 concurrent identical requests | **1 upstream call**, 7 coalesced |
| `bypass_cache` then normal | BYPASS, then HIT |
| Locks left behind after the run | none |
| Stored payloads | zstd-framed, TTLs correct (3600s / 299s / 119s degraded) |
| Compression on a page-sized payload | 22,492B → 271B (**83x**) |
| Redis unreachable | still served results; no stall |

**Through the HTTP endpoint:** 4 requests (miss, hit, hit, bypass) produced only
**2 provider calls**, `/health` reports `{"searxng": "ok", "cache": "ok"}`, and
the metrics confirm it:

```
wss_cache_events_total{namespace="search",outcome="miss"}   1.0
wss_cache_events_total{namespace="search",outcome="hit"}    2.0
wss_cache_events_total{namespace="search",outcome="bypass"} 1.0
wss_search_provider_calls_total{outcome="ok",provider="searxng"} 2.0
```

### Known limitations

- **No cache stampede protection on expiry.** When a hot key's TTL lapses, the
  first request recomputes while others coalesce behind it — correct, but there
  is no proactive refresh-ahead. Fine at this volume.
- **Cross-worker coalescing is poll-based**, not pub/sub. Simpler and adequate;
  a follower's result arrives within one poll interval (20ms–250ms).
- **Negative caching is configured but unused.** `CACHE_TTL_FAILURE` and
  `failure_key()` exist for Phase 3's extraction failures; nothing writes them
  yet. Search failures are deliberately not cached.
- **The `count` parameter is part of the cache key**, so a `count=10` request
  cannot be served from a cached `count=20` result. Callers wanting maximum reuse
  should request a consistent count and slice locally.

---

## Phase 3 — Extraction layer ✅

### Delivered

| File | Role |
|------|------|
| `app/extract/fetch.py` | Size-capped streaming fetch, content-type gate, block detection |
| `app/extract/robots.py` | robots.txt policy, cached per origin |
| `app/extract/trafilatura_ext.py` | Tier 0 — static HTML |
| `app/extract/http_retry.py` | Tier 1 — browser-shaped headers |
| `app/extract/crawl4ai_ext.py` | Tier 2 — headless Chromium |
| `app/extract/firecrawl_ext.py` | Tier 3 — managed, paid |
| `app/extract/router.py` | Escalation, `max_tier` clamping, timeouts |
| `app/services/extract_service.py` | Page cache, negative cache, concurrency |
| `app/api/extract.py` | `POST /extract` |
| `tests/test_extract_fetch.py` | 22 tests |
| `tests/test_extract_router.py` | 29 tests |
| `tests/test_extract_service.py` | 17 tests |
| `tests/test_config.py` | 11 tests |

### The decision that was escalated

The plan named Crawl4AI for tier 2. Before building it, a dependency check showed
it pulls **~70 transitive packages** — scipy, numpy, shapely, trimesh,
alphashape, networkx, nltk, huggingface_hub, tokenizers, openai, litellm, and a
second HTTP stack (`httpx2` alongside our `httpx`) — roughly 150–250MB on top of
Chromium, to render a page and return markdown.

The alternative offered was Playwright directly (one dependency) feeding
trafilatura, which is already tier 0. **The decision was to keep Crawl4AI as the
plan specifies**, and that is what shipped. `pip check` reports no broken
requirements, and the full suite passes with it installed.

The tier still runs trafilatura over Crawl4AI's rendered HTML in preference to
Crawl4AI's own markdown generator, falling back to the latter only when
trafilatura finds nothing. This keeps output shape identical across all four
tiers — a caller should not be able to tell which extractor served a page by
looking at its markdown.

### Key decisions

**trafilatura runs in a worker thread.** It is synchronous and CPU-bound. Called
inline it would block the event loop for every parse, serializing an
`asyncio.gather` fan-out and discarding the concurrency this service is built
on. Measured: 5 pages, 291K chars, 2.87s wall — against ~8s if serialized.

**Streaming with a byte cap, and a content-type gate before the body.** A 50MB
PDF is never downloaded to discover it isn't HTML.

**Block detection is a first-class status.** A Cloudflare interstitial returns
HTTP 200 with a body; treating that as content would return a "successful"
extraction of a bot wall. Markers are matched only in the first 4KB, so an
article discussing CAPTCHAs isn't misclassified.

**Short-but-real content is kept as a fallback.** If every remaining tier fails,
200 characters of real text beats returning nothing.

**Browser concurrency is bounded by RAM, not CPU.** `BROWSER_POOL_SIZE` is a
semaphore around ~1GB-per-context Chromium instances; without it a burst of
browser-tier pages is an OOM.

**Two-level concurrency.** A per-request fan-out cap stops one caller
monopolising the worker; a process-wide semaphore stops N concurrent requests
multiplying into N × fan-out outbound fetches.

**Negative caching is the biggest single cost lever in this phase.** Without it,
every request for a known-bad URL re-pays the entire tier ladder — including the
paid tier — to rediscover the same failure. Measured: 2035ms → 4ms.

**robots.txt failures are permissive.** An unreachable robots.txt is not consent
to be blocked; only an explicit `Disallow` denies.

### The cost bug found during live testing

`/health` showed `firecrawl: ok` and the metrics recorded a Firecrawl call —
despite `FIRECRAWL_API_KEY=` being blank in `.env`. The cause was in the
`.env.example` this project ships:

```
FIRECRAWL_API_KEY=             # blank disables the paid extraction fallback
```

`python-dotenv` parses a trailing comment as the *value* when the value is empty.
So the setting became the literal string `# blank disables the paid extraction
fallback` — truthy — and **silently enabled the paid tier**. The same bug made
`SERVICE_API_KEYS` equal to `# comma-separated, one per consuming app`, which
with auth enabled would have been a valid API key.

Fixes:

1. Every comment in `.env.example` moved onto its own line.
2. A `field_validator` on the credential and engine-list fields treats a value
   beginning with `#` as unset. No real key starts with `#`, so it cannot discard
   a legitimate value.
3. `tests/test_config.py` covers both the regression and a check that the shipped
   `.env.example` parses with no comment-derived values and no paid provider
   enabled by default.

This is the class of bug that shows up as a bill rather than a failure.

### Verified

**Unit — 220 tests passing (79 new).**
- Fetch: size cap and truncation, content-type gating across 4 binary types,
  block detection across 5 interstitial patterns, a false-positive guard, and
  transport failure classification.
- Router: stops at tier 0 on success and never launches a browser; walks tiers in
  cost order; escalates on empty/blocked/timeout/error and on short content;
  `skipped` is terminal; best-partial retention; `max_tier` clamping at each
  level; browser tiers get the longer timeout budget; robots denial fetches
  nothing.
- Service: page cache, canonical-URL sharing, negative cache including
  bypassability and TTL-zero disabling, input-order preservation, duplicate
  collapsing, both concurrency ceilings, and per-page failure isolation.

**Live, against real pages.**

| Check | Result |
|---|---|
| Wikipedia (static) | `trafilatura:ok`, 38,539 chars, 700ms — no browser |
| realpython.com (403s the free tiers) | `blocked → blocked → crawl4ai:ok`, 51,979 chars |
| JS-rendered SPA | `empty → empty → crawl4ai:ok` |
| PDFs (arxiv, orimi) | `skipped: unsupported content-type`, nothing downloaded |
| Markdown quality | headings and links preserved, no nav/footer leakage |
| robots.txt | article allowed, `?action=edit` denied |
| 5 pages in parallel | 291K chars, 2.87s wall |
| Browser warm pool | 1337ms startup once, then ~1.3s/page |
| Browser concurrency | 4 pages at `BROWSER_POOL_SIZE=2`, 2.9s |

**Through the HTTP endpoint:** cold batch 1880ms → warm 5ms; a cold second
process served 3/3 from Redis in 39ms (proving L2, not just L1);
`max_tier=http_retry` finished in 730ms without a browser where `max_tier=crawl4ai`
took 3203ms with one; negative cache turned 2035ms into 4ms.

### Known limitations

- **PDFs are skipped, not extracted.** The content-type gate rejects them. Adding
  a PDF tier is a plausible Phase 9.
- **No per-host rate limiting.** Concurrency is bounded globally and per request,
  but nothing stops 10 concurrent fetches against one origin. Politeness delays
  belong with the Phase 6 anti-blocking work.
- **Crawl4AI writes progress lines to stdout** (`discarding data: …`) that bypass
  structlog. Cosmetic, but it means container logs aren't purely JSON.
- **Firecrawl has never been exercised against the real API** — no key. Its
  behaviour is unit-tested only.
- **`robots.txt` is fetched with the shared client**, so a slow robots.txt eats
  into the request budget before extraction starts.

---

## Verification sweep

Every stage run live against real infrastructure, with all paid providers
configured (Brave, Firecrawl, Anthropic via Vercel AI Gateway, Tavily).

| Level | Result |
|-------|--------|
| Unit suite | **358 passed, ~10s**, slowest test 0.49s |
| Stage 1 — search | **14/14** |
| Stage 2 — cache & single-flight | **18/18** |
| Stage 3 — extraction ladder | **22/22**, incl. Firecrawl tier 3 live |
| Stage 4 — HTTP surface | **29/29**, 0 billable calls |
| Stage 5 — fallbacks, rate limit, LLM | **16/16**, real Brave + real synthesis |
| Benchmarks | see below |
| Container build & run | image builds, all endpoints green, scaling works |

### Bugs the live sweep found that unit tests missed

**1. API keys sharing a prefix collapsed into one identity.** `resolve_key`
returned `f"key:{known[:6]}"`, so `verify-key-1` and `verify-key-2` both became
`key:verify`. They shared a single rate-limit budget and were indistinguishable
in logs. Real keys routinely share prefixes (`sk-proj-…`, a company prefix), so
this would silently merge unrelated consumers. Now a keyed blake2b digest.
The unit test missed it because it used `k1`/`k2`, which differ inside six
characters.

**2. Unreachable URLs were escalating to the paid tier.** A nonexistent domain
walked the whole ladder into Firecrawl, which resolves the same DNS and fails
the same way — money spent to rediscover that a URL does not exist. The router
now only escalates to a billable tier when some earlier tier reported `blocked`
or `empty` (anti-bot or needs-rendering), never on transport errors. Stage 4
went from 1 billable call to 0.

**3. The documented scaling command was broken.** `docker compose up --scale
api=3` fails outright — three containers cannot bind the same fixed host port.
Added `docker-compose.scale.yml` + `deploy/nginx.conf`, verified with three
replicas all serving traffic.

**4. `/search_and_extract` p95 was 43 seconds.** The extract deadline was derived
as `browser_timeout + page_timeout` = 40s, which bounds a single page rather
than the endpoint. Now an explicit `EXTRACT_DEADLINE_S=20`; p95 fell to 20.5s.

**5. The benchmark's cache-hit-rate figure was meaningless.** It reused the same
queries earlier sections had already warmed, reporting a flat 100%. Now uses
fresh queries per run: **79% hit rate, 79% fewer upstream calls**.

### Search quality: the finding that matters most

The plan assumed SearXNG aggregates many engines. Measured from a residential
IP, five of nine are permanently blocked and the free path is effectively
**bing + yep**. Bing alone is not trustworthy — asked "what is a cdn" it
returned Google Classroom, then Panchayat Raj law, then ChatGPT pages across
three consecutive runs, while answering "redis vs memcached" perfectly.

What was tried, and what the numbers said:

| Change | Outcome |
|---|---|
| Add 10 more engines | 11.6 → 95.4 results/query, but **worse answers** and 2.2x latency |
| Enable `braveapi` in SearXNG | Best quality measured, but free tier is ~1 QPS — throttled from 160 to 60 results |
| Force `google` active | Registers, returns 0 results |
| Curate + reweight (chosen) | bing weighted *below* yep, which reads intent better |
| `request_timeout` 4.0 → 3.0 | 4000ms → 2200ms average, keeps yep's contribution |

The honest conclusion: **from a blocked IP the free path is thin, and the fixes
cost money** — residential proxies (`outgoing.proxies`) or a paid search API.
The service is built for this: the Brave fallback engaged correctly and was
*faster* than SearXNG (1298ms vs 4013ms), and it is what makes freshness queries
work at all, since only the blocked `brave` engine supports `time_range`.

**This is the system's binding constraint, and it surfaces downstream.** Chasing
an apparent `/research` defect — answers with zero citations — led back here.
The LLM was behaving correctly:

```
query   "how does oauth2 work"
sources eslteacher.org/do-vs-does, merriam-webster "does", cambridge "does"
answer  "The sources provided do not contain information about how OAuth2
         works. The sources only discuss the English grammar topic of
         'do' vs. 'does'..."
```

Handed junk, the model declines to fabricate — exactly what a grounded-answer
endpoint should do. `/research` quality is therefore capped by search quality,
and no prompt tuning changes that. A citation-recovery path (parsing inline
`[n]` markers when the structured field is empty) was added as defence in depth,
and the stage-5 assertion was corrected: an honest refusal now passes, while an
answer that asserts claims *without* citing anything still fails.

### Benchmarks

| Metric | Value |
|---|---|
| `/search` cold | p50 **413ms**, p95 3370ms |
| `/search` warm | p50 **3ms** (137x) |
| `/search_and_extract` cold | p50 6922ms, p95 12898ms |
| `/search_and_extract` warm | p50 **6ms** |
| Cache economics (80/20 traffic) | **79% hit rate**, 48 requests → 10 upstream calls |
| Extraction success | **18/18 pages** |
| Tier mix | tier 0+1 ~48%, browser ~13%, **paid ~10%** |

Against Tavily on identical queries: Tavily p50 1916ms vs ours 11412ms, and it
returned 4x the content — though it extracts 5 pages to our 3. **Tavily is
faster and will stay faster**; it runs a dedicated extraction fleet while this
renders pages on one box. The trade is latency for marginal cost, and warm-cache
reads (~5ms) are where this service wins outright.

### Containerized run

Image **2.93GB** (Chromium plus Crawl4AI's dependency tree). Verified:

- All four endpoints green, including `/research` returning real citations
- **Chromium runs as non-root** (uid 10001) — 52,225 chars extracted via the
  browser tier inside the container
- Memory 710MB of the 4GB limit under browser load
- SearXNG and Redis **not reachable from the host**, which is what makes
  `limiter: false` safe
- Three replicas behind nginx, with a request cached by one replica served from
  Redis by another in 42ms

---

## Environment notes

- **Host Python 3.14 works after all.** The Phase 0 assumption that trafilatura,
  Playwright, and Crawl4AI lacked 3.14 wheels was wrong — all three install and
  run, `pip check` is clean, and the full suite passes. `requires-python` was
  widened from `>=3.11,<3.14` to `>=3.11`. The container still pins 3.12 as the
  tested target.
- The browser tier needs `python -m playwright install chromium` once.
- `.env` exists locally with a generated `SEARXNG_SECRET` and is gitignored.
- Dev workflow: `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d searxng redis`,
  then run the API from the virtualenv with `SEARXNG_URL=http://127.0.0.1:8080`.

## Phases 4–8 ✅

Built in one pass at the user's request ("continue development, we will test it
all one by one when we are done"), so these carry unit coverage but **no live
verification yet**. `TESTING.md` is the plan for that.

### Phase 4 — Combined pipeline

`app/services/pipeline.py`, `POST /search_and_extract`.

**Deliberately not cached as a unit.** Search results and page extractions are
already cached separately, and page entries are shared across every query that
surfaces the same URL. A composite cache would duplicate that storage and expire
wholesale whenever any single component did.

**`extract_top_k` is the cost lever** — return 10 results, extract the top 3.
**Graceful degradation throughout**: a page that fails, is blocked, or misses the
batch deadline comes back snippet-only rather than failing the request.

`extract_many` gained a `deadline_s` that cancels stragglers and returns them as
`status="timeout"`, so one pathological page costs one result rather than the
whole response.

### Phase 5 — Brave fallback

`app/search/brave.py`. Second in the chain, gated behind `BRAVE_API_KEY`, marked
`billable=True`.

Worth distinguishing from the `brave` engine *inside* SearXNG: that one is
scraped from a shared IP and was rate-limited on our very first query in Phase 1.
This is the paid API with its own index and a dedicated key — which is the whole
reason it's a credible fallback when SearXNG's upstreams block.

Firecrawl (extraction tier 3) already landed in Phase 3; both fallbacks drop out
of their chains entirely when unconfigured.

### Phase 6 — Reliability & anti-blocking

| File | Role |
|------|------|
| `app/common/retry.py` | Exponential backoff + jitter, retryable-only |
| `app/common/useragents.py` | UA rotation with matched client hints |
| `app/common/hostlimit.py` | Per-origin concurrency and pacing |

**Only retryable failures are retried** — a 401 from Brave means the key is
wrong, and three attempts spend three times the latency to fail identically.
Retries sit *inside* the circuit breaker, so a call that succeeds on its second
attempt is one success, and an exhausted retry is one failure.

**Jitter matters more than it looks**: without it, requests that fail together
retry together, reproducing the herd that caused the failure.

**UA rotation ships matched client hints.** A Chrome User-Agent sent with Firefox
hints is a *stronger* bot signal than sending none.

**Per-host politeness** because hammering one origin is the fastest way to earn
the 403 that pushes a page down the expensive tier ladder.

**Proxy support is a separate client** for extraction only — routing internal
SearXNG calls through a residential proxy would be pointless and billed per GB.

### Phase 7 — LLM layer (`/research`)

`app/rerank/llm.py`, `POST /research`. Anthropic (Claude Haiku 4.5) primary,
Ollama alternative.

Three corrections came from checking the current Anthropic API reference rather
than working from assumption:

1. **`claude-haiku-4-5` (the alias)**, not the dated snapshot that was in config.
2. **`effort` is not supported on the Haiku tier** — it errors. Depth control
   here is `max_tokens` and prompt design. There is deliberately no `LLM_EFFORT`
   setting to tempt anyone.
3. **Structured outputs** (`output_config.format` with a JSON schema) replace
   prompting-for-JSON. Guaranteed-parseable output means no retry-on-bad-JSON
   loop — and no retry means no double billing.

`stop_reason` is checked **before** reading content: a refusal returns HTTP 200
with an empty content list, so indexing it blind would raise.

**Citation validation** drops indices outside `1..len(sources)`, plus duplicates,
zero, negatives, and booleans (`bool` is an `int` subclass in Python, so `True`
would otherwise become source 1). A model citing source 7 of 3 is inventing a
reference, and passing it through renders a link to nothing.

**Source truncation** at `LLM_MAX_SOURCE_CHARS` is the main cost lever — ten
unclipped 40k-char pages would be a six-figure-token prompt.

### Phase 8 — Productionizing

`app/common/ratelimit.py` — **cost-weighted** per-key rate limiting.

A flat requests-per-minute cap treats a cached `/search` and a `/research` call
as equivalent, when one is free and the other spends browser time and LLM tokens.
Each endpoint declares a weight instead:

```
/search = 1    /extract = 3    /search_and_extract = 4    /research = 10
```

Counters live in Redis so the budget is shared across replicas — an in-process
limiter would give each replica its own full budget, making the effective limit
N × configured. It fails open: a cache outage must not become an availability
outage.

Auth resolves *before* any budget is charged, so an unauthenticated request
cannot spend another consumer's allowance. With `AUTH_ENABLED=false` every caller
is `anonymous` and the per-key budget collapses into one global budget — the app
logs a warning about this at startup in prod rather than leaving it implicit.

### Verified (unit only — 328 tests)

- Pipeline: merge, `extract_top_k` limiting, `extract=false`, snippet fallback on
  failed extraction, deadline degradation, and that the composite is not cached.
- Brave: gating, freshness mapping, `description`→snippet normalization, auth
  failures non-retryable, 429 retryable, and that **Brave is never called when
  SearXNG succeeds**.
- Reliability: retryable-only retries, retry-inside-breaker, UA/client-hint
  agreement, per-host concurrency and pacing, bounded host tracking, proxy client
  separation.
- LLM: citation range/dedupe/type validation, prompt truncation, provider gating,
  and that the feature flag alone switches off all LLM spend.
- Rate limiting: cost weighting, per-caller isolation, headers, fail-open on a
  dead Redis, and `RATE_LIMIT_PER_MINUTE=0` disabling rather than blocking
  everything.

### Not verified

Everything in Phases 4–8 is unit-tested only. No live run of
`/search_and_extract` or `/research`, no Brave or Firecrawl call against a real
API, no rate limiting against real Redis, and no Docker image build.
`scripts/verify/stage4_pipeline.py` and `stage5_full.py` exist to close this.

---

## Phase 9 — Re-platform onto a paid search provider ✅ (unverified live)

Requested change: drop SearXNG, buy a paid search API, stop crawling every result,
and stop fetching images in the browser tier.

### The finding that reshaped the request

The request was to buy **Brave** as the primary. Checking list prices before
building changed the plan:

| Provider | Price | Index |
|---|---|---|
| Serper | **~$1/1k** at the $50 entry pack, ~$0.30/1k at 12.5M | Google |
| Brave | ~$5/1k, $5/month free credit | Brave's own |
| Tavily | ~$8/1k | own |

Serper is 5x cheaper *and* serves the index whose absence was the root cause of
the quality problem in the first place. Buying Brave as the primary would have
meant paying 5x for the weaker index — and Brave's own index was already what the
scraped `brave` engine inside SearXNG had been serving.

So the chain became **Serper primary, Brave fallback**. Brave was kept rather than
dropped because it is an independent index: a cheaper fallback that also resold
Google would fail in the same conditions that took the primary down. Its $5/month
free credit should cover normal fallback volume, making the insurance free in
practice.

At 200k requests/month with the measured 79% hit rate (~42k upstream calls), that
is ~$42/month on Serper against ~$210 on Brave.

### Delivered

| Change | Detail |
|---|---|
| `app/search/serper.py` | New primary. POST + `X-API-KEY`, `organic[]` → `SearchResult`, Google `tbs` freshness codes, vendor-reported credit metering |
| SearXNG removed | Provider, `searxng/settings.yml`, compose service, dev overlay entry, `SEARXNG_*` config, unresponsive-engines metric, and its test file all deleted |
| `wss_search_credits_total` | New metric, replacing the unresponsive-engines counter. Takes the **provider's own** credit figure, so it counts the double charge on deep result sets that a local call counter would miss |
| Image/font blocking | `--blink-settings=imagesEnabled=false` + `--disable-remote-fonts` on the Chromium tier |
| `DEFAULT_EXTRACT_TOP_K=5` | Omitted `extract_top_k` now means 5, not "extract everything" |
| `DEFAULT_RESULT_COUNT=10` | Was 5. Serper bills one credit for any depth up to 10, so 5 cost the same as 10 |
| Startup validation | Refuses to boot with no search provider; warns when running Brave-only |
| Stage scripts | `spend_notice` up front, `spend_report` at the end, since a sweep now bills real credits |

### Key decisions

**The cache stopped being an optimization and became the cost model.** With a free
primary, a cache miss cost latency. Now it costs money. This reframes several
existing knobs as cost levers — `CACHE_TTL_SEARCH` is a direct multiplier on the
bill — and it means the coalescing behaviour verified in stage 2 (8 concurrent
identical requests → 1 upstream call) is now worth 8x its previous value.

**`extract_top_k` omitted no longer means "all".** The old default made the
deployment's cost ceiling depend on every caller remembering to send the field.
An explicit `0` still means "extract nothing", so unset and zero stay distinct.

**Two credit-boundary facts are encoded in code, not just docs.** Serper bills two
credits above a result depth of 10, and credits expire six months after purchase.
`MAX_FREE_DEPTH` marks the first and a test asserts the shipped default stays under
it; the second appears in the 401/403 error message, because a working deployment
can start failing auth with no config change when a balance runs out.

### The trap found while implementing image blocking

`_BLOCKED_RESOURCES = ("image", "media", "font", "stylesheet")` had been sitting in
`crawl4ai_ext.py` since Phase 3, referenced by a docstring claiming images were
blocked — and **never used**. Images were being downloaded on every browser-tier
page.

The obvious fix was Crawl4AI's `BrowserConfig(text_mode=True)`, which blocks static
resource extensions at the route level. Reading its implementation first
(`browser_manager.py:113`) showed it *also* passes `--disable-javascript`.

That would have quietly destroyed the browser tier. The router escalates to a
browser precisely because a page needed rendering, so a JS-less browser is a
slower, 1GB version of tier 1 — and the failure mode is near-invisible: pages
return `empty` and escalate to the **paid** Firecrawl tier. The symptom is a
larger invoice, not an error.

Setting the two flags directly avoids it. Verified with a standalone Playwright
probe against a media-heavy Wikipedia article:

```
without flags            images= 77  fonts= 0  js_alive=True
with flags               images=  0  fonts= 0  js_alive=True
```

Three tests now guard this: the flags are present, no JS-disabling flag is present,
and `text_mode` does not appear in the module's executable lines.

### Verified

- **Unit suite: 371 passing, ~13s.** Up from 358: a new `test_serper_provider.py`
  (28 tests), startup-validation coverage, `extract_top_k` default coverage, and the
  three browser-flag guards, less the deleted `test_searxng_provider.py`.
- Image blocking measured live: 77 image requests → 0, JavaScript still executing.
- App boots, provider chain composes in the right order, `/health` reports `serper`.
- Every stage script and the benchmark compile and carry the new spend accounting.

### Not verified

**No live Serper call has been made.** No `SERPER_API_KEY` was available, so the
provider is covered by mocked-transport unit tests only. Everything asserted about
Serper's real behaviour — latency, result quality, whether freshness works — is
unmeasured. `scripts/verify/stage1_search.py` was rewritten to check exactly these
and has never run.

Two consequences worth flagging for whoever runs it:

- **Freshness may now work natively.** Under SearXNG only the permanently-CAPTCHA'd
  `brave` engine supported `time_range`, so freshness queries returned nothing.
  Serper passes `tbs` through. Stage 1 checks it; if it passes, a documented
  limitation is gone.
- **The extraction tier mix should improve on its own.** Tier 0+1 at 48% and
  paid-tier at 10% were both worse than target, and both traced to search returning
  junk URLs that were more likely to be anti-bot walls. Better URLs should move both
  numbers without touching the extraction code. That is the measurement that
  confirms or refutes the whole rationale for this phase.

### Also fixed

`TESTING.md` claimed "No per-host rate limiting" as a known gap. It has been
implemented since Phase 6 — `app/common/hostlimit.py`, applied in `ExtractRouter`
around every tier. The same section also repeated four bullets verbatim. Both
corrected; the real limitation (it is in-process, so N replicas means N x the
configured per-host limit) is now stated accurately.

### Live verification — run with a real Serper key

The sweep was run end to end. **All five stages pass: 14/14, 18/18, 22/22, 29/29,
19/19**, plus 375 unit tests and the benchmark.

| Metric | Serper | SearXNG (before) |
|---|---|---|
| `/search` cold | p50 1049ms, p95 2642ms | p50 413ms, p95 3370ms |
| `/search` warm | p50 4ms (236x) | p50 3ms |
| `/search_and_extract` cold | p50 5546ms, p95 14543ms | p50 6922ms, p95 12898ms |
| Cache hit rate (80/20) | 79%, 48 requests → 10 upstream | 79% |
| Projected search cost / 1k | **$0.21** | free, but see quality |
| Tier 0+1 share of successes | 54% | ~48% |
| Paid-tier share of successes | **23%** | ~10% |
| Brave calls, healthy run | 0 | n/a |

**The quality claim held, decisively.** "what is a cdn" now returns Cloudflare,
Wikipedia, AWS, and Akamai. The same query under SearXNG returned Google Classroom,
Panchayat Raj law, and ChatGPT pages on three consecutive runs. **Freshness also
works natively** — 10 results for a past-week query, 8 carrying dates — closing a
limitation that had made every freshness query fall through to the paid fallback.

Cold `/search` p50 got ~2.5x slower (413ms → 1049ms). SearXNG was a container on the
same host; Serper is an internet round trip. p95 improved, warm is unchanged, and
this is the price of the quality jump. Not worth chasing.

### The bug the live sweep found

Stage 5 reported `wss_external_calls_total{provider="brave"} 12.0` — Brave, the ~5x
provider, fired twelve times on a healthy run.

Cause: providers slice results to `count` **before** `SearchService` compares the
count against `MIN_ACCEPTABLE_RESULTS`. The stage's rate-limit burst uses `count=1`,
so every request returned 1 result, was compared against a threshold of 3, was
declared an under-return, and walked the entire provider chain. Worse, the outcome
was flagged `degraded`, which selects `CACHE_TTL_SEARCH_DEGRADED` (120s) instead of
3600s — so this traffic shape re-paid every provider every two minutes.

Any request with `count < MIN_ACCEPTABLE_RESULTS` was unsatisfiable by construction.
The bug predates this phase; with a free primary it escalated free→paid, and the
provider swap turned it into paid→5x-paid.

Fixed by clamping: `threshold = min(count, min_acceptable_results)`. A provider that
returned everything the caller asked for has not under-returned. Four regression
tests, verified to fail without the fix, and the same stage 5 workload now makes
**0 Brave calls**.

This is the fourth serious bug in this project found live while the unit suite was
green, and the second that was purely a cost bug with no functional symptom.

### Two findings that need a decision

**Paid extraction share doubled, contradicting this phase's own prediction.** Tier
0+1 rose 48% → 54% as expected, but Firecrawl's share of successful extractions went
~10% → 23%. The prediction that better URLs would mean fewer anti-bot walls was
wrong: Google's top-ranked results for commercial queries are precisely the sites
that invest in bot protection, whereas SearXNG's junk was low-value and easy to
scrape. Note `max_tier` still defaults to `firecrawl`, so every request may reach the
paid tier — the "deployment owns its ceiling" argument applied to `extract_top_k` was
never applied here. Measured cost of capping at `crawl4ai` on a 9-URL sample: 5/9
extracted instead of 6/9.

**Google surfaces Reddit, and Reddit's robots.txt disallows crawling.** 3 of 9 top-3
URLs in a live sample were Reddit, correctly returned as `skipped`. This explains
extraction success dropping from 18/18 to 13/18 and is *not* a regression. The
inefficiency is that the pipeline selects the top-K before knowing which URLs are
extractable, so each Reddit result consumes one of the five extraction slots and
returns nothing.

### Also corrected

Three cost-meter checks added to stage 5 earlier in this phase short-circuited on
`bool(settings.<key>)`, so with keys configured they passed while printing labels
claiming the provider was inert — one printed "Brave stays inert without a key"
directly above a line showing 12 calls. A check that cannot fail is worse than no
check, because it reads as evidence. All three now assert something real in both
branches, and the LLM check reads token counters rather than `external_calls`, which
the LLM provider never increments.

### Acting on the two findings — measured before and after

Both were implemented and re-measured on the same benchmark.

`DEFAULT_MAX_TIER=crawl4ai` makes the paid tier opt-in per request, mirroring the
argument already applied to `DEFAULT_EXTRACT_TOP_K`: the deployment owns its cost
ceiling rather than trusting every caller to opt out. It resolves through
`resolve_max_tier` in the routes, so an explicit `max_tier=firecrawl` is still
honoured — a default, not a cap.

The pipeline now pre-filters robots-disallowed URLs when choosing extraction
targets, via a new `ExtractService.allows`. Decisions resolve in one concurrent
batch (mostly cache hits, since robots are memoized per process and cached per
origin for 24h), and URLs excluded by policy are reported as `status="skipped"` so a
caller can distinguish "refused" from "not selected".

| Metric | Before | After |
|---|---|---|
| Extraction success | 13/18 slots | **15/18** |
| Tier 0+1 share of successes | 54% | **80%** |
| Paid-tier share of successes | 23% | **0%** |
| Firecrawl calls | 3 | **0** |
| `/search_and_extract` cold p95 | 14543ms | **9356ms** |
| Billable calls | 29 | **26** (all search) |

The two changes compounded in a way neither would have alone: **extraction success
went up while a whole tier was removed.** Capping the ladder loses the pages only
Firecrawl can crack, but the pre-filter recovered more than that by spending the
budget on extractable pages instead of on Reddit URLs that were always going to be
skipped. Both targets in `TESTING.md` — tier 0+1 above 70%, paid below 5% — are met
for the first time.

### The trap the extraction work uncovered

`PROXY_URL` did not reach the browser tier. It configures the shared httpx client,
which covers tiers 0 and 1, but tier 2 launches Chromium and never consulted it.
The gap mattered more than a missing setting usually does: this is the tier reached
*because* a page already refused the cheap fetches, so it is the one most likely to
be facing a block — and the proxy is the documented mitigation for exactly that.
Now passed as `BrowserConfig.proxy_config` rather than the deprecated `proxy` string,
which also cannot carry credentials as structured fields (residential endpoints are
almost always authenticated). **Still unverified against a real proxy.**

### The trap that had quietly broken the test suite

Adding a test that touches `Crawl4AIExtractor` turned an unrelated test into an
order-dependent failure: `test_default_model_is_the_haiku_alias` passed alone and
failed in a full run.

Cause: `import crawl4ai` executes `load_dotenv()` at module scope
(`crawl4ai/config.py:4`), which injects the developer's entire real `.env` into
`os.environ`. Since OS env outranks `_env_file=None`, every `Settings(...)` built
after that import silently reads production credentials and model IDs. `.env` carries
the Vercel gateway's `anthropic/claude-haiku-4.5` spelling, so the assertion on the
first-party alias failed.

This is the same root cause as the documented `ANTHROPIC_BASE_URL` trap — anything
writing to OS env writes to the top of the precedence chain — but with a wider blast
radius, because it was a third-party import doing it rather than the operator.

With the guard removed, **six tests fail**, including
`test_trailing_comment_does_not_enable_the_paid_tier` and
`test_example_ships_no_credentials`. The tests written to prove that no paid provider
is enabled by default were themselves being invalidated by real credentials leaking
in. A suite that claims to need no API keys was one import away from silently using
them.

Fixed in `tests/conftest.py`: a session fixture forces the import before the first
test (guarded, since the browser extra is optional), and an autouse fixture strips
every name `Settings` would read, derived from the model rather than hard-coded so a
new setting is covered automatically. Three tests assert the hermeticity directly.

### Should the browser tier be replaced by Firecrawl entirely?

Proposed on the grounds that it would be lighter and more reliable. Measured on eight
URLs weighted toward pages that defeat the free tiers, all three configurations in one
run:

| config | extracted | billed pages | wall |
|---|---|---|---|
| ceiling `crawl4ai` | 7/8 | 0 | 17.6s |
| ceiling `firecrawl` (full ladder) | 8/8 | **1** | 13.8s |
| no browser, Firecrawl only | 8/8 | **4** | 14.2s |

**The full ladder dominates removing the browser on every axis measured** — identical
8/8 success, a quarter of the billed pages, and marginally faster. The browser tier
absorbed three of the four hard pages for free (cloudflare.com, realpython.com,
dev.to); Firecrawl caught the remainder. So the browser is what keeps the paid share
*low*, not what drives it.

At $0.0008/page (Firecrawl Standard) and top-5 extraction, per 1,000
`/search_and_extract` requests: full ladder ~$0.50, no browser ~$2.00, against $0.21
of search. Removing the tier makes extraction ~10x the search bill instead of ~2x.

The "lighter" half of the claim is real but recoverable: crawl4ai's exclusive
dependency tree measures **556MB** of site-packages (scipy 115MB, patchright 114MB,
playwright 112MB, litellm 71MB, numpy, nltk, PIL) plus 450–700MB of Chromium. The
Dockerfile's existing `EXTRAS` / `INSTALL_BROWSER` args already allow a slim API image
with browser workers on separate replicas, so image weight does not require deleting
the tier.

Latency did not support the proposal either: crawl4ai ran 2.3–2.7s on the hard pages
against Firecrawl's 1.3–2.9s.

`DEFAULT_MAX_TIER` was therefore changed from `crawl4ai` to `firecrawl`, reversing the
cap set earlier in this phase. That cap was based on a 23% paid share measured
**before** the robots pre-filter existed; with wasted Reddit slots removed the full
ladder bills ~12%, and capping costs a page for nothing. The default is safe as
shipped because the paid tier is gated behind `FIRECRAWL_API_KEY` — on an install
without one the tier does not exist, so the ceiling cannot cause spend.

### The real objection, and the metric that answers it

The follow-up question was sharper than the original proposal: if crawl4ai is fetching
from the same VPS or Lambda IP as the free tiers, and that IP gets blocked, what is
left to justify the tier?

It exposed a distinction the metrics could not express. The browser tier does two
separable jobs:

- It **renders JavaScript**. The page served content; a plain fetch just couldn't parse
  it. Entirely IP-independent — a datacenter address cannot stop JS from executing.
- It **beats bot walls** by solving JS challenges. This half is IP-sensitive, and is
  the part that may not survive a move off a residential connection.

`extract_attempts` only showed that crawl4ai succeeded, which cannot tell those apart
— so the question was unanswerable from production data. Added
`wss_extract_rescues_total{extractor, prior_status}`, recorded when a tier succeeds
after a cheaper one failed, labelled with what it rescued the page *from*.

Verified live: `crawl4ai rescued from blocked x2`. Notably, on that run **both**
rescues were the IP-sensitive kind — an earlier run had dev.to as an `empty` rendering
win, but it returned thin-but-ok content the second time. The ratio is unstable across
runs and page mixes, which is exactly why it should not be guessed at.

This cannot be measured from a residential IP, so it stays open with a decision rule
rather than a conclusion: after a week of production traffic, if `blocked` rescues
trend to ~0 and `empty` rescues are negligible, delete the tier and build with
`INSTALL_BROWSER=false`. Until then it is free per-page insurance, and the failure
mode is already graceful — when crawl4ai is blocked the ladder escalates to Firecrawl,
which is the proposed architecture reached automatically on exactly the pages where
the proposal is correct.

---

## Phase 10 — VPS decision, image slimming, and two latency/cost fixes ✅

Deployment target settled on a **single VPS** after pricing both shapes from
measured behaviour (see the cost model): Lambda is cheaper only below ~15,000
requests/month, and above that it loses because it cannot keep a warm browser pool
and must buy from Firecrawl what the browser tier does for free — a 4.2x extraction
bill that swamps Lambda's free compute tier.

### Image: 2.93GB → 2.22GB

Crawl4AI declares the dependencies of every extraction strategy it ships. This
service uses none of them — it drives the browser, takes `cleaned_html`, and runs
trafilatura. Measured by hiding each candidate and running a real browser
extraction against a Cloudflare-protected page (identical output, 9,912 chars):

| Package | Size | Why it was there |
|---|---|---|
| scipy + libs | 135MB | similarity / clustering strategies |
| patchright | 113MB | stealth playwright fork; crawl4ai tolerates its absence |
| litellm | 71MB | LLM extraction strategies |
| networkx | 16MB | link-graph strategies |
| nltk | 13MB | its own chunkers |
| openai | 12MB | pulled in by litellm |
| hf_xet, tokenizers, huggingface_hub | 24MB | model download / token counting |

The prune runs in the **builder** stage and the Dockerfile then re-imports
`crawl4ai` and `app.main` — a bad prune fails the build rather than shipping.

**The layer rule cost a rebuild:** a cleanup step added as a separate `RUN` after
`playwright install` left the image at byte-identical size, because deleting files
in a later layer only writes a whiteout. Cleanup has to happen in the same `RUN` as
the install; the venv prune escapes this only because `COPY --from` copies the
already-pruned result. What remains — Chromium at 656MB plus 271MB of shared
libraries — is irreducible while the browser tier exists.

### Every page was being parsed twice

Investigating a stage 3 failure (parallel extraction at 14.9s against a ~2.9s
baseline, degrading to 60.6s on a second run) showed the cause was not the network:

```
Redis         281KB html   fetch=1032ms   PARSE=3509ms
Web_crawler   346KB html   fetch= 286ms   PARSE=2643ms
HTTP          606KB html   fetch= 198ms   PARSE=4937ms
```

Parsing costs **10-25x** the fetch. And `_extract_sync` ran `trafilatura.extract`
twice per page — once for markdown, once for a `txt` rendering — at **44% of
extraction CPU**.

The second result was unreachable. `ExtractedPage.text` is read in exactly one
place, `char_count = len(page.markdown or page.text or "")`, so only when
`markdown` is absent — but the second parse only ran `if markdown:`. The value was
computed precisely when nothing would read it.

Removing it took the five-page parallel extraction from **14,874ms to 4,622ms**,
and `/research` from 32.2s to 16.3s. Because trafilatura holds the GIL,
`asyncio.to_thread` gives concurrency with the fetches but not parallelism across
parses — redundant parses serialize straight into request latency, five times over
at the default `extract_top_k`. Three tests now assert the parse count.

### The deadline could bill for a page it threw away

The benchmark showed a 22.1s p95 against a 20s `EXTRACT_DEADLINE_S`, with Firecrawl
in the tier mix. `extract_many` cancels pending tasks at the deadline — but
cancelling an in-flight paid scrape does not un-bill it. A page that had already
spent three tiers could reach the paid one with seconds left, be charged, and have
its result discarded.

The batch deadline is now passed down as a monotonic instant, and the router
refuses to start a billable tier when the remaining budget is shorter than that
tier's own timeout. Five tests cover it, including that free tiers are never
budget-gated (skipping them would return nothing at all) and that `/extract`, which
passes no batch deadline, is unaffected.

**Confirmed live on the first containerized run after the fix:**
`wss_extract_escalations_total{from_extractor="firecrawl",reason="skipped_no_time"} 2.0`
— two paid scrapes not started, and not billed.

### Verified

| Check | Result |
|---|---|
| Unit suite | **409 passed** |
| Stages 1-5 | **14/14, 18/18, 22/22, 29/29, 19/19** — 102 checks |
| Image | **2.22GB**, browser tier verified inside it (9,912 chars) |
| Container memory | **622MB** of 4GB under browser load |
| `/health` | all 7 providers ok |
| `/search_and_extract` | 5/5, 5/5, 3/5 extracted; p50 9.5s cold, 39ms warm |
| `/extract` | 52,349 chars via trafilatura, 1.5s |
| `/research` | real Haiku 4.5 call, 2 citations, 16.4s |
| Paid extraction | 0 Firecrawl calls across the run |

### Resource behaviour under sustained browser load — measured

Question worth answering before a VPS deployment: does the browser tier leak memory
or disk across scrapes, given the box has 4GB and a finite disk?

24 consecutive real browser navigations (3 JS-heavy pages x 8 rounds, caching off),
sampled inside the container:

| | |
|---|---|
| RSS after round 1 | 207.9 MB |
| RSS after round 8 | 211.5 MB |
| first-half → second-half average | 212.4 → 213.3 MB (+0.9 MB) |
| drift per scrape | +0.17 MB |
| `/tmp` | 0.2 MB → 0.4 MB |
| `~/.crawl4ai` | 0.0 MB throughout |
| process count | flat at 21 |

**Verdict: stable.** RSS plateaus after the first round rather than accumulating,
`CacheMode.BYPASS` means crawl4ai writes nothing to its own cache directory, and no
Chromium processes are orphaned. The container's writable layer measured 668 kB
after an hour of use. Memory is bounded by `BROWSER_POOL_SIZE` contexts, not by
scrape count, so there is no OOM or disk-exhaustion path from sustained crawling.

### Per-host pacing shipped off

`config.py` carried the comment "Hammering a host is the fastest way to earn the 403
that pushes a page down the expensive tier ladder" directly above
`per_host_delay_s: float = 0.0`. The escalation ladder hits a single origin up to
four times within seconds — trafilatura, http_retry, crawl4ai, firecrawl — which is
precisely that pattern.

It is not theoretical. From a residential IP, back-to-back requests produced:

```
realpython.com  ok     43,698 chars
realpython.com  error       0 chars  Blocked by anti-bot protection: Cloudflare JS challenge
```

Now 0.5s, which costs an escalated page up to +1.5s and nothing at all in the common
case where a request's five pages sit on five different origins.

**The code default alone did not take effect:** `.env` carried the old `0.0`, which
overrides it. The new stage-3 check asserts the *runtime* value rather than the
class default, and caught this immediately.

Stage 3's parallel-extraction check also had to be split. It used five
`en.wikipedia.org` URLs, so it measured off-event-loop parsing and per-host
politeness at the same time and failed the moment pacing was enabled. It now uses
five distinct hosts — the production shape, since search results are rarely
same-origin — and a separate check asserts that same-origin work *is* paced.

### Final verification

| Check | Result |
|---|---|
| Unit suite | **413 passed** |
| Stages 1–5 | **14/14, 18/18, 24/24, 29/29, 19/19** — 104 checks |
| Container memory | 483 MB of 4 GB |
| `/search` | 8 results, 1.8s, no markdown |
| `/search_and_extract` | 5/5, 4/5, 3/5 extracted; p50 5.7s cold, 81ms warm |
| `/extract` | 52,349 chars, 15ms (page-cache hit) |
| `/research` | 4 citations, 10.9s |
| Paid extraction | 0 Firecrawl calls; 3 prevented by the time-budget guard |

---

## Phase 11 — Drop the browser tier; find what it was hiding

Requested: *"lets get rid of crawl4AI and use firecrawl directly, this makes the
system lighter as well."*

The shipped ladder is now `trafilatura → http_retry → firecrawl`. Nothing was
deleted: `app/extract/crawl4ai_ext.py`, the `browser` pyproject extra, and its tests
are intact. Four defaults changed — `ENABLE_BROWSER_EXTRACTOR=false`, the
`EXTRAS`/`INSTALL_BROWSER` build args, and the compose args — so the tier comes back
with `EXTRAS="[browser,llm]" INSTALL_BROWSER=true` plus the env flag. Its tests
`skipif` themselves when the extra is absent, so the suite is green either way.

### The image result beat the estimate by a lot

**2.22GB → 366MB**, an 83% cut against a predicted ~1.0GB. Chromium's binary is
656MB, but `playwright install --with-deps` also drags in a shared-library tail that
turned out to be comparable in size. The browser tier was ~85% of the image.

### Removing it exposed a bug it had been masking

`/search_and_extract` came back with 3 pages and 15,141 chars where the same query
should return 6 and 70,100. The cause was a one-line type of mistake with an
invisible failure mode:

```python
if extractor.tier >= TIER_ORDER[ExtractorName.CRAWL4AI]:
    return self.settings.browser_timeout_s        # 25.0
```

Written when the browser was the top tier, this also caught Firecrawl at tier 3 —
handing a paid API call the browser's 25s budget, **larger than the entire 20s batch
deadline**. The paid-tier time-budget guard from Phase 9 then did exactly what it was
built to do and refused every escalation, because `needed=25` can never fit in
`remaining <= 20`. Firecrawl was unreachable on the combined endpoints *by
construction*.

Nothing failed loudly. Pages returned `empty`, which reads like a page problem.

**The browser tier hid it because it is not billable**, so it never met the guard at
all: it rescued those pages first and Firecrawl was rarely reached. `/extract` passes
no batch deadline, so it was unaffected too — which made the bug look
endpoint-specific rather than like a contradiction between two config values.

The signal was in Phase 10's own final table, recorded as a win:

> | Paid extraction | 0 Firecrawl calls; 3 prevented by the time-budget guard |

Three pages *needed* the paid tier and three were prevented. Read as the guard
working, it was the guard having eaten the tier. A counter that goes up when work is
skipped cannot tell you whether skipping was right; only a test asserting the work is
still reachable can.

### Fix

- `FIRECRAWL_TIMEOUT_S = 15.0`, its own knob. `_timeout_for` now dispatches on
  extractor identity instead of a `>=` tier comparison.
- `EXTRACT_DEADLINE_S` 20 → **25**, leaving ~10s of headroom for the free tiers to
  run before the paid one is budget-gated. Measured free-tier spend on pages that
  escalate is 1–3s — `blocked` and `empty` are fast answers, not hangs — so a
  headroom requirement of a full `PAGE_TIMEOUT_S` would have been over-strict and
  pushed the deadline past the 25s latency cap set in Phase 6.
- Four tests pin it, including one that asserts the paid tier is reachable using
  **the shipped defaults** rather than hand-set ones. The pre-existing test
  `test_browser_tiers_get_the_longer_budget` had been asserting the buggy behaviour
  outright (`FIRECRAWL.last_timeout == browser_timeout_s`); it is now
  `test_each_tier_gets_its_own_budget`.

Verified live: `skipped_no_time` dropped to 0, and the previously-dropped pages
extract — fastly 15,995 chars, aerospike 25,174, youtube 4,903.

### Measured trade

| Metric | Browser OFF | Browser ON |
|---|---|---|
| Image | **366MB** | 2.22GB |
| Unit suite | **420 passed** | 416 |
| Extraction success | **18/18** | 15/18 |
| Tier 0+1 share of successes | 67% (12/18) | 80% |
| Paid-tier share of successes | 33% (6/18) | ~12% |
| `/search_and_extract` cold p50/p95 | 8.6s / **19.9s** | 5.4s / 9.4s |
| `/search_and_extract` warm p50 | 20ms | 4ms |
| Cache hit rate | 79% | 79% |
| Cost / 1k requests | **$0.41** | $0.24 |

Removing the browser costs **+$0.17 per 1k** (69% more extraction spend) and ~10s of
cold p95. It buys a 6x smaller image, no ~1GB-per-context RAM ceiling, and 18/18
extraction instead of 15/18.

The deciding argument was not size, it was the egress IP. The browser's measured wins
were `blocked` rescues, and those are a property of the address rather than the
renderer — on a datacenter VPS the same pages refuse Chromium too. Firecrawl scrapes
from its own egress, so it keeps working where a local browser stops. Paying for that
is paying for the thing the browser could not have provided in production anyway.

Against calling the vendors directly — no cache, no free tiers, every request buying
a search and five scrapes — this is **$0.41 vs $5.15 per 1k, 12.5x cheaper**:
$83/month against $1,030/month at 200k requests.

---

## Phase 12 — Delete the browser tier from the tree

Phase 11 switched the Chromium tier off and kept the code, on the argument that it
cost nothing to leave present and was the right knob if cold p95 ever mattered more
than image size. This phase removes it outright.

**Runtime behaviour is unchanged.** `ENABLE_BROWSER_EXTRACTOR` was already `false`,
so the ladder that ships after this phase is the same three-rung ladder that produced
every Phase 11 baseline. Nothing in the measured-baselines table needed re-deriving.
The change is surface area, not behaviour — which is also why it is verifiable by the
existing suite rather than needing a new benchmark.

### Why "present and off" turned out not to be free

The Phase 11 reasoning was wrong in a specific, checkable way. Keeping a disabled
tier cost:

- **Four tiers' worth of assumptions** in `router.py`, `config.py`, the Dockerfile,
  `docker-compose.yml`, and the test suite — including a `_timeout_for` branch, three
  config settings, an `INSTALL_BROWSER` build knob, a 390MB dependency-prune stage,
  and `shm_size: 1gb`.
- **Conditionally-skipped tests.** `test_extract_router.py` skipped 8 tests whenever
  the `browser` extra was absent, which is the normal install. The suite reported 420
  passing while routinely running 412.
- **An active masking effect.** Traps #12 — Firecrawl unreachable on the combined
  endpoints because it inherited the browser's 25s budget against a 20s deadline —
  survived a whole phase precisely because the non-billable browser tier rescued
  those pages before the paid-tier guard was ever met. A tier that absorbs failures
  is a tier that hides bugs.

The third item is the one that decided it. A hedge that silently degrades the thing
it is hedging is not insurance.

### What was removed

| | |
|---|---|
| Code | `app/extract/crawl4ai_ext.py` (255 lines), `ExtractorName.CRAWL4AI`, its `TIER_ORDER` entry, `_timeout_for`'s browser branch, the `Crawl4AIExtractor` wiring in `main.py` |
| Config | `ENABLE_BROWSER_EXTRACTOR`, `BROWSER_TIMEOUT_S`, `BROWSER_POOL_SIZE` |
| Build | `browser` pyproject extra, `INSTALL_BROWSER` arg, `PLAYWRIGHT_BROWSERS_PATH`, the crawl4ai dependency-prune stage, `/opt/playwright` chmod, compose `shm_size` |
| Tests | `TestBrowserResourceBlocking`, `TestBrowserProxy`, the `needs_browser` skipif, the crawl4ai dotenv-leak imports |
| Deps | crawl4ai plus its orphaned tail: patchright, playwright, playwright-stealth, unclecode-litellm, openai, scipy, networkx, nltk, tokenizers, huggingface_hub, hf-xet, alphashape |

Firecrawl's tier number moved 3 → 2 and `extract_tiers_used` buckets went `(1,2,3,4)`
→ `(1,2,3)`. `TIER_ORDER` is internal ordering only and nothing persists it, so the
renumber is not a migration.

`ExtractorName.CRAWL4AI` was an API-visible enum value: a caller sending
`max_tier: "crawl4ai"` now gets a 422 instead of being silently capped at a tier that
does not run. That is the better behaviour and this is the right time to take the
break, before anything faces a network.

### What replaced the deleted tests

Removing tests to make a change pass is how coverage quietly disappears, so each
deleted class was replaced by one asserting the property that made its bug possible:

- `TestEveryTierIsProxyable` — replaces `TestBrowserProxy`. The original bug was that
  `PROXY_URL` missed the browser tier because Chromium bypassed httpx. The property
  worth pinning is not "Chromium gets the proxy" but "every tier that uses our egress
  shares one configurable client". That is now true by construction, and asserted.
- `test_firecrawl_budget_is_its_own_setting` — replaces
  `test_browser_still_gets_the_browser_timeout`. Traps #12 was a wrong *source*, not a
  wrong number, so the test now moves `PAGE_TIMEOUT_S` and asserts the paid tier's
  budget does not follow.
- `test_no_dependency_reintroduces_the_dotenv_hazard` — replaces the crawl4ai imports
  in `TestSuiteIsHermetic`. Those tests imported the offending library directly, so
  they died with it; the hazard class (a dependency calling `load_dotenv()` at import
  time, OS env outranking `.env`) did not. The new test imports the whole app and
  asserts `os.environ` is still clean.
- The stage-3 script now asserts `crawl4ai` is **not** in the live ladder, so a
  reintroduced browser tier fails verification rather than surprising someone.

### Result

| Metric | After | Before (Phase 11) |
|---|---|---|
| Unit suite | **415 passed, ~35s** | 420 passed, ~62s (412 actually run) |
| Conditionally-skipped tests | **0** | 8 |
| Dev venv | **~250MB** | 678MB |
| Image | 366MB (unchanged) | 366MB |
| Cost / 1k, hit rate, extraction success | unchanged | — |

The suite runs ~27s faster, entirely because `import crawl4ai` is no longer paid at
collection time.

**If cold p95 ever becomes the binding constraint**, the answer is now Open issue #5's
ladder: lower `DEFAULT_EXTRACT_TOP_K` (free, no code), warm the cache for hot queries,
or add a rendering provider behind the existing `ExtractProvider` interface. A managed
one, not a local browser — the container stays small and the egress-IP problem stays
someone else's.

---

## Phase 13 — Close four latent gaps; compress the commentary

Four defects found while reading the tree after Phase 12. None were on the handoff's
open-issues list, and all four were silent: each produced a plausible-looking result
rather than an error.

### 1. An open circuit breaker bypassed the whole ladder

`ExtractProvider.extract` short-circuits when its breaker is open and returned
`status="skipped"`. The router treats `skipped` as TERMINAL — correctly, since robots
and non-HTML are stable properties of a URL — so five transport failures on tier 0
(one network blip, at CIRCUIT_FAIL_THRESHOLD=5) meant every subsequent extraction
returned nothing without trying tiers 1-2.

Worse, the empty result was then written to the negative cache, because
`_record_failure` fires on any non-ok status. A 60-second local fault became **30
minutes of empty answers per URL** (CACHE_TTL_FAILURE), long after the breaker closed.

`status` was overloaded: "no tier will do better" and "this one tier is broken" are
different claims. Added a distinct `unavailable`:

- Not in `_TERMINAL`, so it escalates.
- Not in `_WORTH_ESCALATING`, so it is not grounds to spend — an open breaker taught
  us nothing about the URL.
- Never negative-cached, since it is a fact about us and not about the URL.
- Reported as `outcome="unavailable"` rather than `"error"`, which had made a refusal
  to try look like a URL failure on a dashboard.

No test anywhere referenced `circuit_open` for extraction. `test_skipped_is_terminal`
pinned the terminal behaviour but injected a page-level skip, so the overload was
invisible. Five tests now cover it.

### 2. The paid-tier guard under-reserved by 10 seconds

`firecrawl_ext` set its client timeout to `timeout_s + 10.0` while the router's guard
reserved only `firecrawl_timeout_s`. A scrape could therefore clear the guard with 18s
of budget left, run 25s, get billed, and be discarded by the batch deadline — the exact
waste Traps #10 exists to prevent, reintroduced through an undeclared constant.

Added `ExtractProvider.wall_clock_s(timeout_s)`, the worst-case real time an attempt can
take. Default returns `timeout_s`; Firecrawl adds a declared `_TRANSPORT_MARGIN_S = 2.0`.
The guard reserves that instead of the nominal timeout, so the two numbers cannot drift
apart again. Headroom for the free tiers is now 25 - 17 = 8s, and the headroom test
computes it from `wall_clock_s` rather than the raw setting — subtracting the nominal
timeout overstated it by exactly the slack that caused the bug.

### 3. /extract was unbounded, and its paid-tier guard was inert

The endpoint passed no `deadline_s`, which had two consequences: 20 URLs had no total
time ceiling, and `ExtractRouter` only checks remaining budget when a deadline exists —
so the guard from #2 never ran there at all. That is what made Traps #12 look
endpoint-specific rather than like a config contradiction.

Added `EXTRACT_BATCH_DEADLINE_S=60.0`, separate from the pipeline's 25s because the shape
differs: up to 20 caller-supplied URLs is four waves at EXTRACT_CONCURRENCY=5, against
the pipeline's single wave of DEFAULT_EXTRACT_TOP_K. Reusing 25s would have timed out
most of a full batch. A caller's `timeout_s` is clamped to the batch budget, since no
single page may be given more time than the whole request has.

`tests/test_extract_endpoint.py` is new — the endpoint had **no test file at all**, which
is how both problems survived. It pins the deadline, the clamp, that the deadline still
fits a paid scrape (Traps #12 in its /extract form), and that the deleted `crawl4ai`
tier is rejected with a 422.

### 4. Unextracted results claimed success

`ResultItem.status` defaults to `"ok"`, and the pipeline only overwrote it for extraction
targets. Results ranked below `extract_top_k` therefore returned `status="ok"` with
`markdown=null` — indistinguishable from "we extracted it and the page was empty". A
caller filtering on `status == "ok"` counted results nobody had tried to extract.

The pipeline now initialises every item to `not_attempted` and overwrites per outcome, so
every path out of `run()` leaves a status that means what it says — including the
`extract=false` and `extract_top_k=0` early returns. `skipped` stays reserved for policy
refusals; conflating the two would read a cost ceiling as a robots problem.

One pre-existing test asserted the old behaviour outright
(`test_unselected_results_are_not_marked_skipped` expected `["ok", "ok", "ok"]`). Its
intent was right — unselected results must not be mislabelled `skipped` — but it had
encoded that as the opposite error. Third time in this project a test has been found
pinning a bug; see also Traps #12 and Phase 12's `test_browser_still_gets_the_browser_timeout`.

### Comment pass

The tree was carrying AI-shaped commentary: essay-length rationale blocks, narrative
asides about how findings were made, and restatements of what the code already said.
Compressed across `app/`, `tests/` and `scripts/`, keeping the load-bearing content —
trap warnings, units, and the reasoning behind non-obvious constants — and pushing long
histories here, where they already lived.

| | before | after |
|---|---|---|
| `app/` prose | 31% of lines | **20%** |
| `tests/` prose | 15% | **11%** |
| `config.py` | 323 lines | **271** |

Deliberately NOT stripped: the `Traps #N` cross-references, the `KNOWN GAP` notes now
added to `ratelimit.py` and `robots.py`, and the reasoning at each paid-tier guard. Every
serious bug in this project was found live with the suite green, and those comments are
what made the second occurrence cheap.

### Result

| Metric | After | Before |
|---|---|---|
| Unit suite | **442 passed, ~42s** | 415, ~35s |
| New tests | +27 | — |
| Live stages | **106 checks**, all green | 106 |
| Image | 366MB | 366MB |
| Cost / 1k, hit rate, extraction success | unchanged | — |

Verified live end to end: `trafilatura:blocked -> http_retry:blocked -> firecrawl:ok`
recovering 69,025 chars, `not_attempted` appearing correctly on below-cutoff results, and
`skipped_no_time` still not emitted at all — the paid tier remains reachable.
