# Handoff

Everything a new session needs to pick this up cold. Read this first, then
`PROGRESS.md` for the full build record.

---

## What this is

A self-hosted web search + extraction microservice — the job Tavily does, at a
fraction of the per-call price. Built from `web-search-microservice-plan.md`.

**Status: 13 phases implemented and verified live end to end.** 442 unit tests
passing; all five live stages green (106 checks).

The core idea: managed tools bill one price for three separable jobs — retrieval,
extraction, and LLM synthesis. Here they are separate endpoints, so a request only
pays for the depth it asks for. Everything is cached before anything is bought,
concurrent duplicates are coalesced, and extraction climbs a ladder that only
reaches a paid tier when the free ones are refused.

Measured: **$0.41 per 1,000 `/search_and_extract`**, against ~$5.15 for calling
Serper and Firecrawl directly and ~$8 for Tavily.

### Where it stands right now

**It works and it is verified. It is not yet configured for production.** Three
things are deliberately still open — see Open issues #1, #2 and #4 — and none of
them are code problems:

| Setting | State |
|---|---|
| `ENVIRONMENT` | `dev` |
| `AUTH_ENABLED` | `false`, with zero keys configured |
| Spend ceiling | none — and a third of extractions are now billed |
| `PROXY_URL` | wired to every fetching tier, never tested against a real proxy |

Everything else — the ladder, the cache, the cost meters, the rate limiter, robots
compliance — is built, tested, and measured.

### What changed most recently

**Four silent defects closed (Phase 13).** Each produced a plausible-looking result
rather than an error, and none were on the open-issues list below. Full detail in
PROGRESS.md Phase 13; the parts worth carrying in your head:

1. **An open circuit breaker bypassed the entire ladder** and poisoned the negative
   cache for 30 minutes per URL. `status` was overloaded — an open breaker reported
   `skipped`, which the router treats as terminal. There is now a distinct
   `unavailable` status: it escalates, it is **not** grounds to spend, and it is
   never negative-cached. If you add an `ExtractedPage` status, decide deliberately
   which of those three buckets it belongs in.
2. **The paid-tier guard under-reserved by 10s**, so a scrape could be billed and then
   cancelled — Traps #10 reintroduced through an undeclared `+ 10.0`. The guard now
   reserves `ExtractProvider.wall_clock_s()`. **An extractor that grants itself
   transport slack MUST declare it there.**
3. **`/extract` was unbounded** and its paid-tier guard was inert, because the guard
   only runs when a deadline exists. New `EXTRACT_BATCH_DEADLINE_S=60` (separate from
   the pipeline's 25s — 20 URLs at concurrency 5 is four waves, not one).
4. **Unextracted results reported `status="ok"`** with `markdown=null`, so a caller
   filtering on `ok` counted results nobody tried to extract. They report
   `not_attempted` now. `skipped` stays reserved for policy refusals.

`tests/test_extract_endpoint.py` is new — that endpoint had no test file at all, which
is how #3 survived. A pre-existing test asserted #4's buggy behaviour outright, the
third time in this project a test has been found pinning a bug.

Commentary across `app/`, `tests/` and `scripts/` was also compressed (31% → 20% of
lines in `app/`). Trap cross-references, `KNOWN GAP` notes and the reasoning at each
paid-tier guard were kept deliberately.

**The Crawl4AI/Chromium tier has been deleted, not disabled.** The ladder is
`trafilatura → http_retry → firecrawl` — three rungs, one of them paid. Gone:
`app/extract/crawl4ai_ext.py`, `ExtractorName.CRAWL4AI`, the `browser` pyproject
extra, `ENABLE_BROWSER_EXTRACTOR`, `BROWSER_TIMEOUT_S`, `BROWSER_POOL_SIZE`, the
Dockerfile's `INSTALL_BROWSER` knob and its crawl4ai dependency-prune stage, and
compose's `shm_size`.

**This changed zero runtime behaviour.** The tier was already off, so the shipped
ladder is the same one that produced every measured baseline below — they all still
apply and did not need re-deriving. What changed is surface area.

Why deleted rather than left present-and-off:

- **The deciding argument was the egress IP.** The browser's measured wins were
  `blocked` rescues, and those are a property of the address, not the renderer. On a
  datacenter VPS the same pages refuse Chromium too. Firecrawl scrapes from its own
  egress, so it keeps working exactly where a local browser stops.
- **Keeping it cost more than nothing.** It meant four tiers' worth of assumptions
  across the router, config, Dockerfile and test suite — and it actively masked
  Traps #12 for an entire phase, because a non-billable tier never met the paid-tier
  guard that was silently rejecting every escalation.
- **Its tests were conditionally skipped**, so the usual run covered less than it
  looked like it did. The suite now has no `skipif` at all — every test runs on every
  install.

What the removal bought and cost, measured over Phases 11–12:

- **Image 2.22GB → 366MB.** `playwright install --with-deps` drags in a
  shared-library tail comparable in size to Chromium itself.
- **No ~1GB-per-context RAM ceiling**, no `/dev/shm` sizing, no Playwright version
  drift, no zombie contexts.
- **Extraction success went *up*** — 15/18 → 18/18, because Firecrawl rescues pages
  the local browser could not.
- **More pages are billed**: paid share of successful extractions 12% → 33%. That
  costs +$0.17 per 1k requests and ~10s of cold p95.

**If cold p95 becomes the binding constraint, the answer is no longer "switch the
tier back on".** It is a rendering provider behind the existing `ExtractProvider`
interface — `app/extract/base.py` is the contract, and Firecrawl is the worked
example of a remote one. Re-read anything that assumes four tiers.

Before that, SearXNG was **removed entirely** and Serper became the primary provider
with Brave demoted to fallback. Consequences to hold in your head:

- **There is no free search path any more.** The cache is not a latency optimization,
  it *is* the cost structure. Every miss is a billed call.
- **The stage scripts spend money.** They used to be free to run in a loop. Each
  prints an estimate up front and actual consumption at the end.
- **`extract_top_k` defaults to 5 of 10 results**, so the deployment owns its cost
  ceiling rather than each caller.

---

## Get running in two minutes

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
```

```bash
python -m pytest tests -q
```

Expect **442 passed in ~42s**. The suite is hermetic — no network, no Redis, no API
keys — and has no conditionally-skipped tests, so what you see is the whole of it.
If it takes much longer, something started opening a real socket.

Then the live checks. These need `SERPER_API_KEY` in `.env` **and cost credits**:

```bash
python scripts/verify/stage1_search.py && python scripts/verify/stage2_cache.py && python scripts/verify/stage3_extract.py && python scripts/verify/stage4_pipeline.py && python scripts/verify/stage5_full.py
```

All five print `ALL CHECKS PASSED` — **14/14, 18/18, 26/26, 29/29, 19/19**, 106
checks. Full containerized stack:

```bash
docker compose up -d
```

Scaling (**note the overlay** — plain `--scale api=3` fails, see Traps #7):

```bash
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale api=3
```

---

## Environment

`.env` exists locally and is gitignored. It has working credentials for Serper,
Brave, Firecrawl, Anthropic (via Vercel AI Gateway), and Tavily.
**Never commit it, and never paste its values into chat.** `.env.example` is the
documented template and carries the reasoning for each knob.

Any leftover `SEARXNG_*` entries are harmless (config ignores unknown keys) but can
be deleted.

Local dev runs on Python 3.14 in `.venv`; the container pins 3.12. Both work.

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,llm]"
```

`dev` and `llm` are the only extras, and nothing needs downloading afterwards.

### The knobs that move the bill

| Setting | Default | Why it matters |
|---|---|---|
| `DEFAULT_EXTRACT_TOP_K` | `5` | The single biggest cost lever. Ten instead of five doubles extraction spend *and* adds the slowest page's latency to every request |
| `CACHE_TTL_SEARCH` | `3600` | With a paid primary this is a direct multiplier on the search bill |
| `DEFAULT_MAX_TIER` | `firecrawl` | The whole ladder. Set `http_retry` for a build that can never be billed for extraction |
| `DEFAULT_RESULT_COUNT` | `10` | Serper bills 1 credit up to depth 10 and 2 above it |
| `EXTRACT_DEADLINE_S` | `25.0` | Bounds `/search_and_extract` latency — **and gates the paid tier**, see Traps #12 |
| `EXTRACT_BATCH_DEADLINE_S` | `60.0` | Same for `/extract`, which takes 20 URLs (four waves at concurrency 5) |
| `FIRECRAWL_TIMEOUT_S` | `15.0` | Must stay clear of both deadlines above **by its `wall_clock_s` margin, not by itself** |

---

## Architecture in one screen

```
POST /search              retrieval only          cost weight 1
POST /extract             extraction only         cost weight 3
POST /search_and_extract  retrieval + extraction  cost weight 4
POST /research            + LLM synthesis         cost weight 10   [opt-in]
GET  /health  /livez  /metrics
```

```
request
  ├─ L1 in-process LRU ──────► hit (~0ms)
  ├─ L2 Redis ───────────────► hit (~3ms, shared across replicas)
  └─ miss → single-flight (in-process + Redis lock) → provider   ← this costs money

search      Serper / Google (~$1/1k) → Brave API (~$5/1k, fallback)
extraction  0 trafilatura     free      67% of successes end here or at 1
            1 UA/HTTP2 retry  free
            2 Firecrawl       paid, ~$0.83/1k pages   33% of successes
synthesis   Claude Haiku 4.5 via Vercel AI Gateway   [opt-in only]
```

Cutting across all of it: robots.txt pre-filter, per-host concurrency (2) and pacing
(0.5s), circuit breakers, negative caching, cost-weighted rate limits, Prometheus
cost meters, fail-open Redis.

| Directory | What lives there |
|---|---|
| `app/api/` | Route handlers, one file per endpoint group |
| `app/search/` | `SearchProvider` interface, Serper, Brave |
| `app/extract/` | Tier ladder, router, robots policy, size-capped fetch |
| `app/cache/` | Codec (orjson+zstd), L1, Redis L2, single-flight, layer |
| `app/common/` | Circuit breaker, retries, rate limit, UA rotation, host limits |
| `app/services/` | Orchestration: search, extract, pipeline |
| `app/rerank/` | LLM synthesis for `/research` |
| `scripts/verify/` | Live stage scripts + benchmark (not run by pytest, cost money) |

Every layer sits behind an interface (`app/search/base.py`, `app/extract/base.py`,
`app/rerank/base.py`), so swapping a provider is config, not a rewrite.

---

## Decisions already made — don't re-litigate

| Decision | Why |
|---|---|
| Serper primary | ~$1/1k and Google-backed: cheapest *and* best. Unusual, but that is what the pricing says |
| Brave kept as fallback | Independent index, so it fails differently than the primary; $5/mo free credit usually covers it |
| SearXNG removed entirely | Quality was a property of the egress IP, not of this code. Cost more in junk results and paid extraction than it saved |
| **Browser tier deleted entirely** | 2.22GB → 366MB, no ~1GB-per-context RAM ceiling, extraction success 15/18 → 18/18. Its wins were `blocked` rescues, which erode on a datacenter IP anyway |
| Not kept present-and-disabled | It was, for a phase, and that was not free: four tiers' worth of assumptions across router/config/Dockerfile/tests, conditionally-skipped tests, and it masked Traps #12 for a whole phase. A future rendering need goes behind `ExtractProvider`, not into this container |
| Sized for 10k–200k queries/month | 4 vCPU / 8GB target; the current build fits 2 vCPU / 4GB below ~50k |
| Caching at Phase 2, not 4 | It shapes the service interfaces — and is now the cost model |
| Valkey instead of Redis | BSD-licensed drop-in; one-line swap back |
| Claude Haiku 4.5 for `/research` | Cheap API tier; `effort` is NOT supported on this tier |
| One uvicorn worker per container | Keeps Prometheus counters correct; scale via replicas |
| Rate limiting weighted by endpoint cost | A cached `/search` and an LLM call are not equivalent |
| Composite pipeline deliberately uncached | Components cache separately and page entries are shared across queries |
| Default 10 results, extract top 5 | 10 is free at the search layer; extraction is the part that costs per page |

---

## Traps that already cost time

Each was found the hard way and is documented inline at the relevant code too.

1. **`.env` trailing comments become values.** `python-dotenv` parses
   `KEY=    # note` as the value `# note`. This silently enabled the paid Firecrawl
   tier once. Comments go on their own line; a validator now rejects `#`-leading
   credential values. The blast radius grew with the provider swap — a comment
   parsed as `SERPER_API_KEY` boots fine and then 401s every search.

2. **OS env outranks `.env`.** The setting is `LLM_BASE_URL`, *not*
   `ANTHROPIC_BASE_URL` — the latter is set globally by Claude Code and the
   Anthropic SDK, and would silently send a gateway key to `api.anthropic.com`.

3. **Gateway model IDs differ.** Vercel uses `anthropic/claude-haiku-4.5` (dots);
   first-party uses `claude-haiku-4-5` (hyphens).

4. **~~Crawl4AI's `text_mode=True` also disables JavaScript.~~** *No longer
   applicable — the browser tier is gone.* Kept because the shape recurs: a flag
   named for the thing you want (block images) also did a thing you did not
   (disable JS), which defeated the only reason that tier existed. The symptom was
   a **bill**, not an error, because pages came back `empty` and escalated to the
   paid tier. When you next reach for a convenience flag on a fetching layer, read
   what else it turns off.

5. **Serper bills two credits above a result depth of 10.** `count: 20` doubles the
   per-query cost. A test asserts the shipped default stays under the boundary.

6. **Serper credits are prepaid and expire after six months.** A deployment can start
   401/403-ing with no config change at all when a balance runs out. The provider's
   error text says so explicitly, which is the only reason it was diagnosable.

7. **`docker compose up --scale api=3` fails** — three containers can't bind one host
   port. Use `docker-compose.scale.yml`.

8. **A request for fewer results than `MIN_ACCEPTABLE_RESULTS` used to bill every
   provider.** Providers slice to `count` before `SearchService` compares against the
   threshold, so `count=1` returned 1 result, was judged to have under-returned
   against a threshold of 3, and walked the whole chain. It was then cached under the
   120s *degraded* TTL instead of 3600s, so it re-paid every two minutes. Found live:
   a burst of `count=1` requests billed 12 Serper credits **and** 12 Brave calls at
   ~5x. Fixed by clamping to `min(count, threshold)`; four tests guard it, and the
   same workload now makes 0 Brave calls.

9. **Parsing costs 10–25x more than fetching, and it was being done twice.**
   `trafilatura.extract` takes 2.3–4.9s on a large page against 125–1030ms to fetch
   it. `_extract_sync` ran it a second time to populate `ExtractedPage.text` — 44% of
   extraction CPU for a value nothing could read (`text` was only computed when
   `markdown` existed, and only read when it didn't). Removing it took the 5-page
   parallel extraction from 14.9s to 4.6s. Because trafilatura holds the GIL,
   redundant parses do not vanish into the thread pool; they serialize into request
   latency, five times over at the default `extract_top_k`.

10. **The batch deadline could cancel an already-billed Firecrawl call.**
    `extract_many` cancels pending tasks at `EXTRACT_DEADLINE_S`, but cancelling an
    in-flight paid scrape does not un-bill it. A page that burned three tiers could
    reach the paid one with seconds left, get charged, and have its result thrown
    away. The router now refuses to start a billable tier without enough remaining
    budget. Watch `wss_extract_escalations_total{reason="skipped_no_time"}` — **and
    read Traps #12 before concluding a non-zero value there is good news.**

11. **Deleting files in a later Docker `RUN` does not shrink the image.** It writes a
    whiteout; the bytes stay in the earlier layer. A cleanup step added after
    `playwright install` left the image byte-identical. Cleanup must run in the same
    `RUN` as the install — or in the builder stage, where `COPY --from` copies only
    the result (which is why the 390MB venv prune worked).

12. **Firecrawl inherited the browser's timeout and became unreachable.** *The most
    expensive bug in this project's history — read this one even though the tier that
    hid it is gone.*
    `_timeout_for` keyed off `tier >= crawl4ai`, written when the browser was the top
    tier, so Firecrawl got the browser's 25s budget — *larger than the entire 20s
    batch deadline*. The paid-tier guard from #10 therefore rejected **every**
    escalation: `needed=25` never fits in `remaining <= 20`. Firecrawl was unreachable
    on the combined endpoints by construction.

    Nothing failed loudly. Pages returned `empty`, which reads like a page problem.
    It hid because the browser tier is **not billable**, so it never met the guard and
    rescued those pages first; and because `/extract` passes no deadline, so it was
    unaffected and the bug looked endpoint-specific. Dropping the browser exposed it
    instantly: `/search_and_extract` returned 3 pages / 15,141 chars where it should
    have returned 6 / 70,100.

    The signal had already been recorded — as a *win*, in Phase 10's own summary:
    "0 Firecrawl calls; 3 prevented by the time-budget guard." Three pages needed the
    paid tier and three were prevented.

    Fixed with a per-extractor budget (`FIRECRAWL_TIMEOUT_S=15`) and
    `EXTRACT_DEADLINE_S=25`, leaving ~10s of headroom for the free tiers. Four tests
    pin it, one using the *shipped* defaults rather than hand-set ones; a pre-existing
    test had been asserting the buggy behaviour outright.
    **The general lesson: a counter that goes up when work is skipped cannot tell you
    whether skipping was right. Only a test asserting the work is still reachable can.**

    **Nothing masks a recurrence now.** The non-billable tier that used to rescue
    those pages is gone, so `TestPaidTierTimeBudget` is the only thing standing
    between the ladder and silently losing its top rung. Do not weaken it, and keep
    `_timeout_for` keyed per-extractor rather than by a `tier >= N` comparison —
    the wrong *source*, not the wrong number, was the actual defect.

13. **`import crawl4ai` ran `load_dotenv()` at module scope.** *The dependency is
    gone; the hazard class is not.* It injected the whole real `.env` into
    `os.environ`, and OS env outranks `_env_file=None` — so the moment any test
    imported it, every later `Settings(...)` in the session silently picked up real
    credentials. It surfaced as an order-dependent failure (a test passing alone,
    failing in a full run) and had invalidated six tests, including the ones asserting
    no paid provider is enabled by default. `tests/conftest.py` still strips every
    settings name per test, and `test_no_dependency_reintroduces_the_dotenv_hazard`
    now asserts that importing the app leaves `os.environ` clean. Same root cause as
    #2: anything writing to OS env writes to the top of the precedence chain.

14. **~~`PROXY_URL` did not reach the browser tier.~~** *Resolved by removal rather
    than by a fix.* Chromium bypassed the shared httpx client entirely, so the
    documented mitigation for a blocked IP missed the one tier reached *because* a
    page had already refused the cheap fetches. Both remaining fetching tiers share
    the extraction httpx client, and the paid tier uses Firecrawl's egress rather than
    ours, so the setting now covers everything that uses our address by construction.
    `TestEveryTierIsProxyable` pins that property. **Still unverified against a real
    proxy** — see Open issue #4.

15. **A config default is inert if `.env` overrides it.** `PER_HOST_DELAY_S` was fixed
    in `config.py` and stayed 0.0 at runtime because `.env` carried the old value. The
    stage checks now assert the *runtime* value, not the class default.

---

## Open issues, in priority order

### 1. Not configured for production

`ENVIRONMENT=dev` and `AUTH_ENABLED=false` with zero API keys configured. Both need
flipping before anything faces a network. This is the only item on this list that is
purely mechanical — it is #1 because it is cheap and blocking.

### 2. No spend ceiling — and it matters more than it used to

Nothing caps cumulative spend. The rate limiter bounds per-key *request* rate, not
money, and `bypass_cache` is caller-controlled — a loop that sets it turns the 79%
hit rate into 0% and the bill from $0.41/1k into $1.97/1k.

This was a footnote when extraction was mostly free. With the browser tier gone,
**33% of extractions are billed**, so the exposure is real.

Options: a hard monthly credit budget checked before dispatch, or refusing
`bypass_cache` from unprivileged keys. **Needs a decision on what happens when the
budget is hit — 429, or degrade to cache-only?**

### 3. Extraction blocking is real NOW, and worse on a VPS

Not a future risk — it already happens on a residential IP. In one run
realpython.com returned a clean 43,698-char extraction and then
`Blocked by anti-bot protection: Cloudflare JS challenge` on the **next** request. In
the last live run, 4 of 5 extraction targets were `blocked` at both free tiers.

A datacenter IP is scored worse than a residential one, so expect this to increase
after deploying. What limits the damage:

- Pacing (0.5s) and per-host concurrency (2) slow how fast a host is provoked.
- The negative cache stops a known-bad URL being re-attempted for 30 minutes.
- **Blocked pages fall through to Firecrawl, which scrapes from its own egress** — so
  blocking converts into cost rather than failure. That is the designed behaviour and
  the reason the paid tier is not really optional.

The cost consequence is direct: if blocking rises on a VPS, the paid share rises with
it and `$0.41/1k` drifts upward. Watch
`wss_extract_rescues_total{prior_status="blocked"}` and the Firecrawl share, and
compare against the 33% baseline before concluding anything regressed.

### 4. The proxy path is still unverified

`PROXY_URL` reaches every fetching tier, but **no proxy has ever been tested through
it.** It is the main lever if #3 gets worse after the move.

Costing already done: break-even against Firecrawl is ~$2.50/GB, and at 200k req/mo a
residential proxy saves only ~$11/mo — so this is an availability lever, not a cost
one. Note `PROXY_FOR_EXTRACTION` routes **all** traffic, which is ~3x more expensive
than just paying Firecrawl. A retry-only proxy mode is the version worth building,
and it does not exist yet.

### 5. Cold latency is structural

8.6s p50 / 19.9s p95 on cold `/search_and_extract`. Tavily is ~1.9s; they run a
dedicated extraction fleet. This got ~10s worse at p95 when the browser tier was
dropped, because pages that were rescued locally now make an API round trip.

Warm cache (20ms) is where this service wins, and 79% of traffic is warm.

**If cold p95 turns out to matter, "re-enable the browser tier" is no longer the
answer** — the tier is deleted, and it was deleted knowing this. The options, in
order of how much they cost you:

1. **Lower `DEFAULT_EXTRACT_TOP_K`.** Cold p95 is the slowest of K pages; K=3 cuts
   both latency and bill. Cheapest lever, available today, no code.
2. **Warm the cache for known-hot queries** on a schedule. Turns cold requests into
   warm ones for the traffic that matters, and the 79% hit rate says that shape of
   traffic exists.
3. **A rendering provider behind `ExtractProvider`** — Firecrawl is the worked
   example of a remote one, so this is a new file and a `TIER_ORDER` entry, not a
   redesign. Choose a managed one over a local browser: the container stays small
   and the egress-IP problem stays someone else's.

### 6. Quality-based fallback trigger — probably no longer needed

The Brave fallback fires on result *count* (`< 3`). The original argument for a
quality-based trigger was that a blocked engine produced junk that looked successful.
Serper returns 7–10 relevant results per query with Brave never firing, so **the
problem this would have solved appears to be gone.** Don't build it without fresh
evidence; every fallback is a second paid call.

### 7. Smaller items

- `RateLimiter` never consults the Redis circuit breaker, so a Redis outage costs a
  ~2s socket timeout on *every* request in the limiter while cache calls
  short-circuit correctly. Same class as the 13-second stall already fixed.
- `RobotsPolicy._parsed` is unbounded and never expires per process, so the 24h Redis
  TTL is defeated by a process-lifetime memo. `HostLimiter` bounds its equivalent map;
  this doesn't. It matters more now that the pipeline consults robots for every
  candidate result. Flagged as a `KNOWN GAP` in the module docstring.
- Negative cache is checked *before* the page cache, costing an extra Redis GET per
  extraction and letting a stale failure shadow a valid 24h page entry.
- ~~`/extract` passes no batch deadline~~ — **fixed in Phase 13**
  (`EXTRACT_BATCH_DEADLINE_S`), which also activated the paid-tier guard there.
- **Ruff has never actually run on this project.** It is declared in the `dev` extras
  but was not installed; installing it surfaces 47 findings, all pre-existing and
  overwhelmingly `E501` plus unused `noqa`. Nothing functional, but the lint gate in
  `pyproject.toml` is currently decorative.
- No load testing; verified at 8 concurrent and 3 replicas only. The 3-replica run has
  not been repeated since the browser tier was dropped, though it should only be
  easier now.
- Container memory not re-measured (was 622MB of 4GB under browser load; it can only
  have fallen). The compose limit is deliberately still 4g — lowering an untested
  ceiling trades a known-safe margin for a guess. Measure the real page-cache
  working set, then trim.
- Redis memory growth unmeasured under a real page-cache working set.
- PDFs are skipped, not extracted.
- Ollama provider implemented but never exercised (`LLM_PROVIDER=anthropic`). Safe to
  delete if you'll never use it.

---

## Measured baselines

Compare against these before concluding something regressed. The "Browser ON" column
is the last run that had the tier enabled, kept so the trade is visible rather than
asserted.

**These did not move when the browser code was deleted**, because the tier was
already switched off — the "Now" column was measured on the same three-rung ladder
that ships today. Only the unit-suite row changed, and only because browser-only
tests went with it.

| Metric | Now | Browser ON |
|---|---|---|
| Unit suite | **442 passed, ~42s** | 416, ~62s |
| Live stages | **106 checks**, all green | 102 |
| Image | **366MB** | 2.22GB |
| `/search` cold | p50 **974ms**, p95 **2308ms** | p50 1049ms, p95 2642ms |
| `/search` warm | p50 **11ms** (85x) | p50 4ms |
| `/search_and_extract` cold | p50 **8625ms**, p95 **19950ms** | p50 5375ms, p95 9356ms |
| `/search_and_extract` warm | p50 **20ms** | p50 4ms |
| Cache hit rate (80/20 traffic) | **79%**, 48 requests → 10 upstream | 79% |
| Extraction success | **18/18 slots** | 15/18 |
| Tier 0+1 share of successes | **67%** (12/18) | 80% |
| Paid-tier share of successes | **33%** (6/18) | ~12% |
| Search cost / 1k | **$0.21** | $0.21 |
| Total cost / 1k | **$0.41** | $0.24 |
| `/research` tokens per call | **8,691 in / 342 out** | — |
| Brave calls, healthy run | **0** | 0 |
| Container memory | not re-measured | 622MB of 4GB under browser load |

**The trade, in one line:** extraction success 15/18 → **18/18** and a 6x smaller
image, paid for with **+$0.17 per 1k** (69% more extraction spend) and ~10s of cold
p95. Warm latency is unchanged in kind and the cache still absorbs 79% of traffic,
which is why the total only moves 17 cents.

Still **12.5x cheaper than calling Serper and Firecrawl directly** ($0.41 vs $5.15
per 1k): $100/mo all-in against $1,030/mo at 200k requests. The saving is the 79% hit
rate, request coalescing, and the 67% of pages that never reach a paid tier.

**Paid-tier share is 33% and that is intended, not a regression.** It went 10% → 23%
early on (contradicting the prediction that better URLs would reduce it), was driven
to ~12% by capping `DEFAULT_MAX_TIER` and pre-filtering robots-disallowed URLs, then
rose to 33% *by design* when the browser tier was removed — the same pages, now
scraped by API instead of by local Chromium. Judge it against extraction success
(18/18) and total cost/1k ($0.41), not against its own history.

**Cold `/search` p50 is ~2.5x slower than under SearXNG** (413ms → 974ms): SearXNG was
a container on the same host, Serper is an internet round trip. p95 improved, warm is
unchanged, and it bought a total transformation in result quality — "what is a cdn"
returns Cloudflare, Wikipedia, AWS and Akamai, where SearXNG served Google Classroom
and Panchayat Raj law across three consecutive runs. Not worth chasing.

### The three metrics that decide the bill

All exported from day one:

- `wss_search_credits_total` — the search bill, taken from the **vendor's own**
  reported credit usage rather than counted locally, so it catches the double charge
  on deep result sets that a call counter would miss.
- `wss_cache_events_total` — hit rate. Every hit is a search you didn't buy.
- `wss_external_calls_total{billable="true"}` — every paid call, every layer.

Plus two that answer "is the ladder still healthy":
`wss_extract_rescues_total{extractor,prior_status}` and
`wss_extract_escalations_total` — where a non-zero `skipped_no_time` means the paid
tier is being budget-gated away (Traps #12).

---

## Working agreements

- **Verify against the live system, not just unit tests.** Every serious bug in this
  project was found live while the suite was green: a 13-second Redis stall, a paid
  tier enabled by a comment, API keys colliding into one identity, a 43-second p95,
  and a paid tier that had been unreachable for a whole phase.
- **Check the current API reference before writing provider code.** The `claude-api`
  skill caught three errors that would have shipped, including `effort` being
  unsupported on Haiku.
- **Measure before tuning.** "More engines" seemed obviously right and was wrong. So
  was "buy Brave, it's the paid one" — Serper turned out to be 5x cheaper *and*
  better-sourced. So was the ~1GB image estimate, which was off by 3x.
- **A silent skip is not a success.** Guards that avoid work need tests proving the
  work is still reachable, or they quietly become feature removals.
- Docs are load-bearing: `PROGRESS.md` (build record + findings), `TESTING.md` (how to
  verify + what "healthy" looks like), `README.md` (what it is + how to run),
  `web-search-microservice-plan.md` (the original plan).

---

## Suggested first move in a new session

```bash
python -m pytest tests -q
```

442 green means the tree is sound. From there the work is **deployment, not
features** — the extraction and search layers are verified and the cost model is
measured.

In order: flip `ENVIRONMENT` and `AUTH_ENABLED` with real keys (#1), decide the
spend-ceiling behaviour (#2), then prove `PROXY_URL` against a real proxy *before*
you need it (#4). Everything in #7 is optional cleanup.

If cold latency turns out to be the blocker rather than cost, read Open issue #5 —
the browser tier is gone, and lowering `DEFAULT_EXTRACT_TOP_K` is the lever that
costs nothing to try.
