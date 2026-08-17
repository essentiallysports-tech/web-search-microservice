# Web Search Service

A self-hosted web search + extraction microservice. Same job as Tavily or a
provider's built-in web search, at a fraction of the per-call price.

## Why it's cheap

The managed tools bill one price for three separable jobs:

1. **Retrieval** — ranked URLs + snippets for a query
2. **Extraction** — a page turned into clean, LLM-ready markdown
3. **Synthesis** — an optional LLM pass that structures or summarizes

Most callers need (1), or (1)+(2). Here they are separate endpoints, so a request
only pays for the depth it asks for. (3) is opt-in per request and never runs
implicitly.

Three things then drive the per-request cost down:

- **A cheap retrieval provider.** Serper resells Google's index at ~$1/1k
  (~$0.30/1k at volume) against Tavily's ~$8/1k.
- **Cache everything.** Every cache hit is a search you didn't buy. Measured at
  79% on a realistic 80/20 repeat distribution, which takes the effective search
  cost to roughly a fifth of list price.
- **Extraction that is genuinely free for most pages.** The tier ladder escalates
  cheapest-first: 67% of successful extractions resolve on the two free tiers, and
  only the remainder reach the paid scraper.

Concurrent duplicate requests are also coalesced into a single upstream call, so a
popular query going cold doesn't produce a thundering herd of paid calls.

Measured, end to end: **$0.41 per 1,000 `/search_and_extract`** — $100/month all-in
at 200k requests, against ~$1,030 for calling Serper and Firecrawl directly and
~$1,600 for Tavily. The gap is the 79% cache hit rate, the 67% of pages that extract
free, and the coalescing.

> **An earlier version of this service ran SearXNG as a free primary.** That was
> removed, and the reasoning is worth knowing before you consider adding it back:
> SearXNG has no index of its own, so its quality is a property of your egress IP.
> From a residential IP, five of nine engines were permanently CAPTCHA'd and Bing
> intermittently returned results for an unrelated query. From a datacenter IP it
> is worse. The free path cost more in result quality — and in the paid extraction
> it triggered downstream — than it saved. `PROGRESS.md` has the full measurements.

## Architecture

```
                    ┌──────────────────────────────────────┐
   POST /search ───▶│  L1 in-process LRU → L2 Redis        │
   /extract         │  single-flight (dedupes concurrent    │
   /search_and_…    │  identical misses into one call)      │
   /research        └──────────────┬───────────────────────┘
                                   │ miss
                  ┌────────────────┴────────────────┐
                  ▼                                 ▼
         ┌─────────────────┐            ┌───────────────────────┐
         │ SEARCH          │            │ EXTRACTION (tiered)   │
         │ Serper  ~$1/1k  │            │ 0 trafilatura   free  │
         │   ↓ fallback    │            │ 1 UA/HTTP2 retry free │
         │ Brave   ~$5/1k  │            │ 2 Crawl4AI  off, opt-in│
         └─────────────────┘            │ 3 Firecrawl     paid  │
                                        └───────────────────────┘
                                                   │ opt-in only
                                        ┌──────────▼────────────┐
                                        │ SYNTHESIS             │
                                        │ Claude Haiku / Ollama │
                                        └───────────────────────┘
```

Every layer sits behind an interface (`app/search/base.py`,
`app/extract/base.py`, `app/rerank/base.py`), so swapping a provider is config,
not a rewrite.

### Why Brave is second and not gone

Brave is five times Serper's unit cost, which makes it a poor primary. It stays as
the fallback because it runs its **own index** rather than reselling Google — a
cheaper fallback that resold the same upstream would fail in the same conditions
that took the primary down. It only fires when Serper errors or under-returns,
which normally fits inside Brave's $5/month free credit, so the insurance is
usually free.

If Brave's share of `wss_search_provider_calls_total` starts climbing, that is not
a cost to absorb quietly — it means Serper is failing and you are paying 5x for it.

### The tier that earns its keep

Tier 1 — retry the plain HTTP fetch with a realistic User-Agent and HTTP/2 — exists
because a large share of pages that *look* like they need JavaScript are just
User-Agent gated. Catching those before paying anyone is the difference between a
200ms free fetch and a billed API call.

Measured on real pages, the ladder behaves as intended:

```
en.wikipedia.org   trafilatura:ok                                            700ms
realpython.com     trafilatura:blocked → http_retry:blocked → firecrawl:ok    ~7s
JS-rendered SPA    trafilatura:empty   → http_retry:empty   → firecrawl:ok    ~7s
*.pdf              skipped by the content-type gate, nothing downloaded       230ms
```

Wikipedia costs nothing to extract. That is the whole extraction cost argument in
one line. Measured over a benchmark run: **67% of successful extractions resolve on
the two free tiers**, so the paid tier is charged for roughly one page in three.

### Why there is no browser tier

A headless-Chromium tier (Crawl4AI) used to sit between tier 1 and Firecrawl. It
was switched off, and then **removed from the tree entirely** — there is no
`ENABLE_BROWSER_EXTRACTOR`, no `browser` extra, and no `crawl4ai_ext.py`.

The trade is explicit. A local browser is free per page, so dropping it means
paying Firecrawl for pages it used to rescue — roughly 4x more billed pages in the
benchmark mix. Against that:

| | with browser | without (shipped) |
|---|---|---|
| Image | 2.22GB | **366MB** |
| RAM ceiling | ~1GB per concurrent context | none |
| Failure modes | Chromium OOM, zombie contexts, `/dev/shm` sizing, Playwright/browser version drift | one HTTP call |

The deciding argument is *where the request comes from*. The browser's measured
wins were `blocked` rescues, and those are a property of the egress IP — on a
datacenter VPS the same pages block Chromium too, because the IP is what gets
refused, not the renderer. Firecrawl scrapes from its own egress with its own
rotation, so it keeps working exactly where a local browser stops.

Measured on the benchmark: the paid tier's share of successful extractions went
12% → **33%**, which costs **+$0.17 per 1,000 requests** — $34/month at 200k, less
than the VPS RAM the browser wanted. Extraction success went the other way,
15/18 → **18/18**, because Firecrawl rescues pages the browser could not.

What it costs is latency: cold `/search_and_extract` p95 went 9.4s → **19.9s**,
since those pages now make a real API round trip. Warm requests are unaffected and
79% of traffic is warm.

**This is a one-way door, deliberately.** The tier was kept present-but-disabled
for a while as a hedge against cold p95 mattering, and that hedge was not free: it
meant four tiers' worth of assumptions in the router, the config, the Dockerfile
and the test suite, and it actively masked a bug that made the paid tier
unreachable for a whole phase (see `TestPaidTierTimeBudget`). If cold p95 turns out
to be the binding constraint, the answer now is a rendering provider behind the
existing `ExtractProvider` interface — not a browser in this container.

Extraction targets are also chosen with policy in mind. The pipeline checks the
cached robots decision *before* spending an extraction slot, because Google ranks
Reddit highly and Reddit disallows crawling — without the pre-check, up to 3 of 5
slots went to URLs that were always going to be skipped. Filtering them first raised
extraction success from 13/18 to 15/18 while *removing* the paid tier.

Each page is parsed **exactly once**. Parsing dominates this service — 2.3-4.9s on
a large article against 125-1030ms to fetch it — and the extractor used to run
trafilatura twice per page to derive a plain-text field nothing read. Removing the
second pass cut 44% of extraction CPU and took a five-page parallel extraction from
14.9s to 4.6s. A test asserts the parse count, because the cost is invisible in
output correctness.

Image bytes are never fetched at all: both free tiers ask for HTML and the
content-type gate refuses anything that isn't, so a media-heavy article costs its
markup and nothing else.

## Endpoints

| Endpoint | Layers | Rate-limit cost | Cost profile |
|---|---|---|---|
| `POST /search` | retrieval | 1 | cheapest |
| `POST /extract` | extraction | 3 | medium |
| `POST /search_and_extract` | retrieval → parallel extraction | 4 | medium |
| `POST /research` | + LLM synthesis | 10 | highest, opt-in |
| `GET /health` | per-provider status | — | — |
| `GET /metrics` | Prometheus | — | — |

Callers bound their own spend with `max_tier` (stop before the paid API) and
`extract_top_k` (extract only the top few results).

By default a combined request returns **10 results and extracts the top 5**
(`DEFAULT_RESULT_COUNT` / `DEFAULT_EXTRACT_TOP_K`). Ten is free at the search
layer — Serper bills one credit for any depth up to 10 — while extraction is the
part that actually costs per page, so the two defaults are set independently.
An omitted `extract_top_k` means the configured default, **not** "extract
everything": the deployment owns its cost ceiling rather than trusting every
caller to send the field.

Rate limiting is a **budget of cost units per minute per API key**, not a flat
request count — so a consumer can make many cheap calls or a few expensive ones.
`/research` costs ten times a `/search` because it is the only path that spends
LLM tokens.

`/research` returns **503 unless `ENABLE_LLM_LAYER=true`** and a provider is
configured. A default install cannot be billed by it.

## Running it

```bash
cp .env.example .env
```

`SERPER_API_KEY` is **required** — it is the primary provider and the service
refuses to boot without it (or a Brave key). Serper gives 2,500 credits free to
trial at <https://serper.dev>.

```bash
docker compose up --build
```

Redis is the only local dependency; search is an external API.

### A note on cost, read this before tuning

Three settings move the bill more than anything else:

- **`DEFAULT_EXTRACT_TOP_K`** — extraction is the dominant per-request cost once
  search is a flat ~$0.001. Crawling ten results instead of five doubles it and
  adds the slowest page's latency to every request.
- **`DEFAULT_MAX_TIER`** — `firecrawl`, i.e. the whole ladder is allowed. There is
  no tier between `http_retry` and the paid one, so capping here means hard pages
  simply come back unextracted. Set it to `http_retry` if you want a build that can
  never be billed for extraction, and let callers opt in per request with
  `max_tier: firecrawl`.
- **`CACHE_TTL_SEARCH`** — with a paid primary, the cache is not a latency
  optimization, it *is* the cost structure. A shorter TTL is a direct multiplier
  on the search bill.

`EXTRACT_DEADLINE_S` and `FIRECRAWL_TIMEOUT_S` interact, and getting the
relationship wrong disables the paid tier silently. The router will not *start* a
billable scrape it cannot finish inside the remaining budget, because cancelling an
in-flight Firecrawl call does not refund it. So if `FIRECRAWL_TIMEOUT_S` crowds
`EXTRACT_DEADLINE_S`, every escalation is refused and pages come back `empty` —
which reads like a page problem, not a config one. The shipped 15s against 25s
leaves ~10s for the free tiers to run first; tests pin both ends of it. The symptom
to watch for is `wss_extract_escalations_total{reason="skipped_no_time"}` above zero.

Two more that are easy to get wrong:

- **Serper charges two credits for a result depth above 10.** `MAX_FREE_DEPTH` in
  `app/search/serper.py` marks that boundary, and a test asserts the shipped
  default stays under it. A caller passing `count: 20` doubles its own query cost.
- **Serper credits are prepaid and expire six months after purchase.** Buying far
  ahead of measured volume loses money rather than saving it. A working deployment
  can also start returning 401/403 with no config change at all when a balance
  runs out — the provider's error message says so explicitly for that reason.

### Local development

The container pins Python 3.12 as the tested target, but the full stack —
including trafilatura, Playwright, and Crawl4AI — installs and runs on 3.14 too.

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev,llm]"
```

`dev` and `llm` are the only extras. There is no browser to install and nothing to
download after the pip install.

```bash
python -m pytest tests -q
```

The suite needs no network, no Redis, no browser, and **no API keys** — every
external dependency is faked. Bring up Redis only for end-to-end work:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
```

Note the stage scripts under `scripts/verify/` **do** spend real credits, unlike
the unit suite. Each one prints an estimate before it starts and what it actually
consumed when it finishes.

## Cost model

| Path | Cost |
|---|---|
| Serper search | ~$1/1k queries, less at volume — reduced by the cache hit rate |
| Brave fallback | ~$5/1k, only when Serper fails or under-returns |
| trafilatura + HTTP retry extraction | free — 67% of successful extractions |
| Firecrawl fallback | per-page, 33% of successes (anti-bot and JS-rendered) |
| Redis | one instance |
| `/research` | per-call LLM tokens, opt-in |

Three metrics decide the bill, and all are exported from day one:

- `wss_search_credits_total` — the search bill, taken from the **provider's own**
  reported credit usage rather than counted locally, so it accounts for the
  double charge on deep result sets that a call counter would miss.
- `wss_cache_events_total` — hit rate. Every hit is a search you didn't buy.
- `wss_external_calls_total{billable="true"}` — every paid call across all layers.

The cache is doing real work: in an end-to-end run, four `/search` requests
(miss, hit, hit, bypass) produced two provider calls. A warm hit returns in ~0ms
versus ~1.2s cold, and eight concurrent identical requests collapse to a single
upstream call.

`wss_search_provider_calls_total` is the honesty check on the provider mix: if
Brave's share climbs, Serper is failing and each query costs 5x what it should.

## Scaling

Sized for 10k–200k queries/month on 4 vCPU / 8GB.

One uvicorn worker per container, scaled with replicas — this keeps Prometheus
counters correct without the multiprocess collector, and makes the growth path
obvious. Scaling needs the overlay, because the base file publishes a fixed host
port and three containers cannot bind it:

```bash
docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d --scale api=3
```

That puts nginx in front and round-robins across replicas via Compose's embedded
DNS. Verified: three replicas all served traffic, and a request answered by one
replica was served from cache by another in 42ms — the Redis cache is shared,
not per-process. That sharing is now a cost property as well as a latency one.

The image is **366MB**, and there is only one image — no browser variant to build,
tag, or keep on separate replicas. A replica is cheap to start and cheap to hold in
RAM, which is what makes `--scale api=N` the whole scaling story.

## Operating a crawler

This service fetches pages on your behalf, which makes you a crawler operator.
`RESPECT_ROBOTS_TXT=true` is the default. Rate limits, `User-Agent` honesty, and
the terms of the sites you extract from are your responsibility.

Note this applies to **extraction only**. Retrieval is now an API call to a provider
that authenticates your key, so nothing about search makes you a crawler.

### Extraction still fetches from your IP — plan for it

Moving search to an API removed *search-side* blocking completely: Serper and Brave
want your traffic and authenticate your key. It did nothing for extraction. Every
free tier — trafilatura and the UA retry — fetches the target page directly from
your egress IP, and that exposure is real and current, not theoretical: in the last
benchmark 4 of 5 extraction targets were `blocked` at both free tiers.

This is the reason the paid tier is not optional in practice. Firecrawl scrapes from
its own egress with its own rotation, so it is the only rung of the ladder whose
success does not depend on your address. A local browser was tried and did not
help enough to keep: Chromium presents a more convincing client, which defeats
User-Agent gates, but Cloudflare-class protection correlates on address reputation,
and by the time the ladder reaches its third rung the host has already refused you
twice from that same address. **Expect this to be worse on a VPS than in
development** — datacenter ranges are pre-scored, and development here ran from a
residential connection.

What the service already does about it:

| Mitigation | Where |
|---|---|
| Rotate 5 real, current browser UA profiles with matching client hints | `common/useragents.py` |
| Full navigation header sets (`Sec-Fetch-*`, `Referer`) — what UA gates actually check | `extract/http_retry.py` |
| Firecrawl fallback, which fetches from its own IPs rather than yours | `extract/firecrawl_ext.py` |
| Per-origin pacing (0.5s between request starts), so the ladder doesn't hammer one host | `common/hostlimit.py` |
| Per-origin concurrency cap, so escalating doesn't multiply load on a host that just refused | `common/hostlimit.py` |
| Circuit breaker + negative cache, so a blocked host isn't re-attempted every request | `common/circuit.py`, `services/extract_service.py` |

None of that changes your address, so none of it is a fix — they reduce how quickly
you earn a block, not whether the block applies.

**The actual fix is `PROXY_URL`.** It routes extraction fetches — and only those —
through an upstream proxy, leaving search API traffic direct (those providers have no
reason to block you, and proxy bandwidth is billed per GB). Residential providers
generally expose one rotating endpoint, so a single URL is enough.

It reaches every tier that uses your address, which is now true by construction:
both free tiers fetch through the shared extraction httpx client, and the paid tier
does not use your egress at all. That used to be a real gap — the setting configured
only the httpx client, so the local-browser tier silently went out direct, and that
was precisely the tier reached *because* a page had already refused the cheap
fetches. Removing the browser closed the gap rather than patching it.

**The proxy path is implemented but has never been tested against a real proxy.**
If extraction success falls after deploying to a VPS, this is the first thing to
try, and the first thing to verify actually works.

## Layout

```
app/
├── main.py              FastAPI app, lifespan, middleware
├── config.py            every cost/latency knob, env-driven
├── models.py            request/response + internal schemas
├── http_client.py       one shared httpx client per process
├── security.py          API-key auth
├── cache/keys.py        query + URL normalization (decides hit rate)
├── common/circuit.py    per-provider circuit breaker
├── common/metrics.py    Prometheus: cache, provider mix, spend
├── search/base.py       SearchProvider interface
├── search/serper.py     primary — Google via Serper
├── search/brave.py      fallback — independent index
├── extract/base.py      ExtractProvider interface + tier ladder
├── rerank/base.py       LLMProvider interface
└── services/            orchestration
```

**Picking this up in a new session? Start with [HANDOFF.md](HANDOFF.md)** — status,
two-minute startup, decisions already made, the traps that cost time, and the
open issues in priority order.

| Doc | Purpose |
|---|---|
| `HANDOFF.md` | Start here — orientation and what to do next |
| `PROGRESS.md` | Build record: what each phase delivered and why |
| `TESTING.md` | How to verify, and what "healthy" looks like |
| `web-search-microservice-plan.md` | The original plan |
