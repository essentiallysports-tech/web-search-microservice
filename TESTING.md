# Test Plan

What to run, in what order, and what "correct" looks like — so verification at
the end of development is a checklist rather than a memory exercise.

Three levels:

| Level | What | Speed | Needs | Costs money |
|-------|------|-------|-------|-------------|
| 1 | `pytest` — hermetic unit/contract suite | ~60s | nothing | no |
| 2 | `scripts/verify/stage*.py` — real infra, real websites | ~1–3 min each | Redis + network + key | **yes** |
| 3 | `scripts/verify/benchmark.py` — latency, cache economics, cost | ~3–5 min | Redis + network + key | **yes, ~80 queries** |

Level 1 proves the logic. Level 2 proves the integration. **Every serious bug so
far was found at level 2 or 3, not level 1** — the suite passed green while a
Redis outage added 13 seconds per request, and while a `.env` parsing quirk had
silently enabled a paid provider. Do not treat a green suite as verification.

> **Levels 2 and 3 now bill real credits.** They used to run through a local
> SearXNG and were free to loop. Each script prints an estimate before starting and
> what it actually consumed at the end. A full sweep is roughly 20 search queries,
> under $0.05 at list price — cheap, but no longer zero, so don't put it in a watch
> loop.

---

## Prerequisites

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,llm]"
```

```bash
cp .env.example .env
```

`SERPER_API_KEY` is required for levels 2 and 3 — it is the primary search
provider, and the service refuses to boot without it or a Brave key. 2,500 free
trial credits at <https://serper.dev>. Level 1 needs no keys at all.

Bring up Redis (the dev overlay publishes it on loopback so the app can run from
the virtualenv). Search is an external API, so this is the only local dependency:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
```

---

## Level 1 — automated suite

```bash
python -m pytest tests -q
```

**Expected: 502 passed, ~46s.**

> ⚠️ `tests/` is **gitignored** — it is kept locally and not pushed, so a fresh clone has
> no suite and CI cannot run it. Whoever holds this working copy holds the only copy.
> Run it before every push; nothing else will.

No network, no Redis, no API keys — every external dependency is faked. If this
takes much longer than a minute, something started opening a real socket.

There are no conditionally-skipped tests. Every test in the suite runs on every
install, which is a property worth keeping: the previous suite skipped its
browser-tier tests whenever the optional extra was absent, so the usual run
silently covered less than it appeared to.

| File | Covers |
|------|--------|
| `test_keys.py` | Query normalization, URL canonicalization, key sensitivity |
| `test_config.py` | `.env` parsing guards, no keys shipped, startup validation, credit-depth default |
| `test_serper_provider.py` | Normalization, POST body, `tbs` freshness, failure classification, credit metering |
| `test_search_service.py` | Provider chain, fallback, partial-result rescue, small-count non-fallback |
| `test_search_endpoint.py` | `/search` contract, auth, health status codes |
| `test_cache_primitives.py` | Codec, L1 TTL/LRU, single-flight |
| `test_cache_layer.py` | L1/L2 ordering, coalescing, fail-open, Redis breaker |
| `test_search_caching.py` | Envelope round-trip, TTL policy, key sensitivity |
| `test_extract_fetch.py` | Size cap, content-type gate, block detection, single-parse guard |
| `test_extract_router.py` | Tier escalation, `max_tier`, per-tier timeouts, robots, rescue accounting, paid-tier time budget |
| `test_extract_service.py` | Page cache, negative cache, concurrency, ordering |
| `test_pipeline.py` | Search→extract merge, `extract_top_k` incl. the default-of-5, deadline degradation |
| `test_brave_provider.py` | Brave gating, normalization, fallback chain ordering |
| `test_reliability.py` | Retries, UA rotation, per-host limits, proxy client |
| `test_llm_layer.py` | Citation validation, prompt truncation, provider gating |
| `test_ratelimit.py` | Cost weighting, per-caller isolation, fail-open |

**What level 1 does NOT cover:** real network behaviour, real latency, real
upstream blocking, Redis under load, container memory, or anything about cost.

---

## Level 2 — stage verification

Run in order. Each prints `[PASS]`/`[FAIL]` per check and exits non-zero on any
failure. Baselines in brackets are what was measured during development on a
residential connection — treat large deviations as signal, not gospel.

### Stage 1 — search layer

```bash
python scripts/verify/stage1_search.py
```

**Last run: 14/14 passed.** Freshness confirmed working natively — the check
SearXNG could never pass.

Checks:
- A real query returns results, and at least 5 for a common query
- Results normalize: all URLs http(s), unique, titles and snippets present
- Every result is labelled `engine="google"` — provenance is the index, not the
  reseller
- **Freshness works natively.** This is the check SearXNG could never pass: only
  its permanently-CAPTCHA'd `brave` engine supported `time_range`, so
  freshness-constrained queries returned nothing and fell through to the paid
  fallback. Serper passes Google's `tbs` codes through, so a past-week query should
  return results. If it does, a documented limitation is gone.
- The default result count stays inside one Serper credit (depth ≤ 10)
- Cold search latency **[Serper advertises 1–2s; no local measurement yet]**
- Chain composition: Serper primary, Brave second when keyed
- Health reports `serper: ok`

The engine-availability checks that used to dominate this stage are gone with
SearXNG. Result quality is now a property of Google's index rather than of the
egress IP, which is the entire point of the switch.

### Stage 2 — caching and single-flight

```bash
python scripts/verify/stage2_cache.py
```

Checks:
- Cold miss → warm hit **[~1200ms → ~0ms]**
- A second `CacheLayer` with a cold L1 still hits via Redis **[~3ms]** — proves
  L2 works, not just process memory
- `"  Python  TYPE hints guide? "` hits the entry created by
  `"python type hints guide"` — normalization payoff
- Different `count` / `lang` / `freshness` each MISS
- 8 concurrent identical requests → **exactly 1 upstream call**, 7 coalesced
- `bypass_cache` refreshes rather than poisons; the next caller gets a hit
- No `:lock` keys left behind
- Compression on a page-sized payload **[≥3x; measured 83x on prose]**
- **Regression guard:** with Redis unreachable, a request completes in
  **< 8s [~2s]**. This once took ~13s because a failed lock was mistaken for
  "another worker is computing", so the request waited out the full
  single-flight timeout with 2s socket timeouts per poll.

Every check in this stage is now also a cost check. The coalescing result — 8
concurrent identical requests producing exactly 1 upstream call — is the difference
between one paid search and eight.

### Stage 3 — extraction ladder

```bash
python scripts/verify/stage3_extract.py
```

Checks:
- **The shipped ladder is three rungs and `crawl4ai` is not one of them** — asserted
  outright, so a reintroduced browser tier fails the stage rather than surprising you
- **Static page resolves at tier 0 and never reaches a paid tier** — this single
  check is the entire cost argument **[~700ms, >10k chars]**
- Markdown has headings and links, with no nav/footer leakage
- UA-gated page (`realpython.com`) escalates
  `trafilatura:blocked → http_retry:blocked → firecrawl:ok` **[~3.6s]**
- JS-rendered SPA escalates `empty → empty → firecrawl:ok`
- PDFs are `skipped` by the content-type gate — **never downloaded** **[~230ms]**
- `max_tier=http_retry` stops before the paid tier
- robots.txt: article allowed, `?action=edit` denied
- 5 pages in parallel **[~4600-5400ms wall]** — meaningfully less than serialized,
  proving trafilatura is running off the event loop.
  The old ~2900ms baseline was never reachable: parsing a large Wikipedia article
  costs 2.3-4.9s and trafilatura holds the GIL, so five of them cannot finish in
  under three seconds on any core count. Before the double-parse was removed this
  check measured 14.9s and, under load, 60.6s.
- Same-origin requests are paced, not hammered — asserts the *runtime*
  `PER_HOST_DELAY_S`, not the class default, because `.env` can make a fixed config
  default inert

There is no `--no-browser` flag any more, and no browser warm-up step: the ladder is
`trafilatura → http_retry → firecrawl` on every install.

**This stage costs money** when `FIRECRAWL_API_KEY` is set — the paid tier is
exercised for real, which is the point. It hits live third-party sites. A failure may mean a site changed rather than
the code broke — the ladder trail printed per page tells you which.

### Stage 4 — full HTTP surface

```bash
python scripts/verify/stage4_pipeline.py
```

Checks:
- `/health` reports `serper`, `cache`, and extractor tiers; **`firecrawl` is
  absent without a key** (must not cost money by default)
- `/search` returns results with **no markdown and no extractor** — the cheap
  endpoint stays cheap
- `/search` second call is a `hit`
- `/extract` batch: all extracted, warm batch fully cached
- `/extract` preserves input order and collapses duplicates
- Negative cache: a genuinely failing URL is not re-attempted **[2035ms → 4ms]**
- robots-disallowed URL returns `skipped`
- `/search_and_extract` with `extract_top_k=3`: 5 results, ≤3 with markdown, the
  rest snippet-only
- `extract=false` returns snippet-only with `attempted == 0`
- **Search spend tracks distinct queries, not request count.** Four
  search-bearing requests over two distinct queries must produce at most two
  provider calls. Zero is also correct — a re-run inside `CACHE_TTL_SEARCH` finds
  both queries warm, which asserts the same property.
- **The paid extraction tier never fires on ordinary pages** (`firecrawl` == 0)

Note the old assertion here was "no billable external calls during the run", which
is no longer achievable or meaningful: search itself is paid, so a working run
*must* spend. The invariants above are what actually protect the bill.

### Stage 5 — fallbacks, reliability, rate limiting, LLM

```bash
python scripts/verify/stage5_full.py
```

Checks:
- **Provider chain composition** — Serper is primary, Brave second when
  `BRAVE_API_KEY` is set and absent when it isn't. Note an absent Brave no longer
  means "this install cannot spend money", since the primary is paid too — it means
  there is no independent-index fallback, which is a resilience gap
- Auth: missing key → 401, wrong key → 401, valid key → 200
- **Cost-weighted rate limiting**: `X-RateLimit-*` headers present, a burst of
  `/search` eventually 429s with `Retry-After`, and a second key still has its
  own budget
- **`/research` returns 503 while `ENABLE_LLM_LAYER=false`** — it must be unable
  to spend anything on a default install. With a key set, it instead asserts
  every returned citation points at a real source (no hallucinated indices)
- UA rotation produces more than one profile over 200 draws
- **The two genuinely expensive tiers stay inert unless configured** — Firecrawl,
  Brave, and the LLM layer each spend nothing without their key

Brave, Firecrawl, and the LLM layer are exercised only when their keys are
present; otherwise the script verifies they are correctly *inert*.

---

## Level 3 — benchmarks

```bash
python scripts/verify/benchmark.py
```

```bash
python scripts/verify/benchmark.py --compare-tavily   # needs TAVILY_API_KEY
```

Reports over a fixed 10-query set:

1. `/search` cold latency (p50/p95/max)
2. `/search` warm latency and speedup
3. `/search_and_extract` cold, top-3 extracted, plus extraction success rate
4. `/search_and_extract` warm
5. **Cache economics** under an 80/20 repeat distribution: hit rate, upstream
   calls made, calls avoided
6. **Extraction tier distribution** — what share of pages resolve free vs. paid
7. **Projected cost per 1,000 requests**, against Tavily (~$8/1k) and Brave
   (~$5/1k) list prices
8. Optional head-to-head with Tavily on identical queries: latency and content
   volume

### What to look for

Baselines are from the last full sweep, with all providers configured.

| Signal | Healthy | Last measured | Worrying |
|--------|---------|---------------|----------|
| Cache hit rate (80/20 traffic) | > 60% | **79%** | < 40% — check key normalization |
| Extraction success | > 80% of slots | **18/18** | < 60% |
| Tier 0+1 share of successes | > 55% | **67%** (12/18) | < 40% — the free path has stopped working, check for IP blocking |
| Paid-tier share of successes | < 40% | **33%** (6/18) | > 55% — free tiers are being refused; the lever is `PROXY_URL`, not a code change |
| Total projected cost / 1k | < $1.00 | **$0.41** | > $2 |
| `firecrawl` escalations `skipped_no_time` | **0** | **0** | any sustained count — `FIRECRAWL_TIMEOUT_S` is crowding `EXTRACT_DEADLINE_S` and the paid tier is being budget-gated away. This shipped once and looked like "pages are empty" |
| Brave share of search calls | ~0% | **0** | any sustained share — Serper is failing and you're paying 5x |
| Search credits per 1k requests | < 300 | **208** | > 500 — the cache is not earning its keep |
| `/search` cold p50 | < 1.5s | **974ms** | > 3s |
| `/search` warm p50 | < 30ms | **11ms** | > 200ms — Redis or L1 sizing |
| `/search_and_extract` cold p95 | < 25s | **19.9s** | > 25s — the batch deadline is truncating results |
| `/search_and_extract` p95 | < 25s | **9.4s** | > 30s — check `EXTRACT_DEADLINE_S` |

**The paid-tier target was relaxed from <5% to <15% deliberately.** It briefly hit
23% after the provider swap, because Google's top results for commercial queries are
exactly the sites that invest in bot protection, whereas SearXNG's junk was low-value
and scraped easily. The robots pre-filter — which stopped Reddit results consuming
extraction slots — brought it to ~12% on the full ladder, and measurement showed that
capping below the paid tier is the wrong way to push it lower:

| ceiling | extracted | billed pages |
|---|---|---|
| `http_retry` | 4/8 | 0 |
| `firecrawl` (default) | 8/8 | 4 |

Capping below the paid tier does not make hard pages cheaper — it makes them fail.
The 33% paid share is the price of the other 4/8, and it is the intended trade.

**The two rescue rows are how you tell WHY the paid tier is being reached**, which is
a different question from how often. Rescues from `empty` are JS-rendering wins and
are IP-independent: that share should not move when the deployment moves. Rescues
from `blocked` are bot walls Firecrawl's own egress got through — the IP-sensitive
half. A rising `blocked` share after deploying to a VPS means your address is being
scored worse, which converts directly into bill, and `PROXY_URL` is the lever for it.

Extraction success reads 15/18 rather than 18/18; the remaining 3 are Reddit URLs,
correctly `skipped` because its robots.txt disallows crawling. Not a regression.

Search credits per 1k and the Brave share are the cost thesis in numbers: the first
is the bill, the second tells you whether you're paying 1x or 5x for it.

---

## Deployment verification — passed

```bash
docker compose build
```

```bash
docker compose up -d
```

Verified on the last run:
- [x] Image builds — **366MB**. Historical: 2.93GB with Chromium, 2.22GB after
      pruning 390MB of unused Crawl4AI deps in the builder stage, then 366MB once
      the browser was dropped. Chromium plus its `--with-deps` shared libraries was
      ~85% of the image, which is why removing it was worth more than any pruning.
- [x] `/health` green inside the container network, all providers
      (`crawl4ai` correctly absent from the list — it no longer exists to report)
- [x] Redis **not reachable from the host**
- [x] Ladder resolves 5/5 mixed-difficulty pages: 2 trafilatura, 3 firecrawl
- [x] `wss_extract_escalations_total{reason="skipped_no_time"}` is **0** — the paid
      tier is actually reachable within the batch deadline
- [x] All four endpoints green, `/research` returning real citations

### Scaling

`docker compose up --scale api=3` **fails** — three containers cannot bind the
same fixed host port. Use the overlay, which fronts the replicas with nginx:

```bash
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale api=3
```

- [x] Three replicas all served traffic
- [x] A request cached by one replica was served from Redis by another in 42ms,
      proving the cache is shared rather than per-process

---

## Known gaps

Everything except the search layer has been run live with all paid providers
configured. What remains unproven:

- **No spend ceiling exists.** Nothing caps cumulative cost. The rate limiter
  bounds per-key request rate, not spend, and `bypass_cache` is caller-controlled,
  so a caller looping with it set bills every call.
- **No load testing.** Concurrency is bounded by construction and verified at
  small scale (8 concurrent, 3 replicas); behaviour at hundreds of concurrent
  requests is unmeasured.
- **No sustained-run measurement.** A 3-minute benchmark cannot see how the
  numbers drift over hours or from a datacenter IP. This matters less than it did
  — upstream engine blocking was the thing that degraded over time, and that is
  gone with SearXNG — but extraction-side blocking still applies.
- **Redis memory growth is unmeasured** under a realistic page-cache working set.
- **PDFs are skipped, not extracted.**
- **Ollama provider never exercised** — implemented per the plan, but
  `LLM_PROVIDER=anthropic` means it has never run.

Per-host rate limiting was previously listed here as missing. It is **not** missing:
`app/common/hostlimit.py` implements per-origin concurrency and optional pacing, and
`ExtractRouter` applies it around every tier. The real limitation is narrower — it is
in-process, so with N replicas the effective per-host limit is N x the configured
value.

---

## Quick reference

Free, no keys needed:

```bash
python -m pytest tests -q
```

Costs ~20 search credits, needs `SERPER_API_KEY` and Redis:

```bash
python scripts/verify/stage1_search.py && python scripts/verify/stage2_cache.py && python scripts/verify/stage3_extract.py && python scripts/verify/stage4_pipeline.py && python scripts/verify/stage5_full.py
```

Costs ~80 search credits:

```bash
python scripts/verify/benchmark.py
```
