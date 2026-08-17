# Web Search Microservice — Build Plan

A self-hostable, cost-effective web search + data-collection microservice that gives you
Tavily/AI-provider-grade retrieval power without per-call pricing that burns credits.
Designed to be reused as a shared backend across multiple apps.

---

# PART 1 — APPROACH & TOOLS

## Core principle

The expensive tools (Tavily, AI-provider web search) bundle three separate jobs into one
billed call. We split them so each request only pays for the depth it needs, cache
aggressively, and self-host the parts that are free to self-host.

The three jobs:

1. **Retrieval** — get ranked URLs + snippets for a query.
2. **Extraction** — turn a page into clean, LLM-ready text/markdown.
3. **Rerank / clean** — optional LLM step for structuring or summarizing results.

Most requests need only (1) or (1)+(2). Job (3) is opt-in per request, never baked into
every call. This is the single biggest cost separator vs. the managed tools.

## Design rules

- **Layered & swappable.** Each layer sits behind an interface; swapping a provider is a
  config change, not a rewrite.
- **Cache first.** Every cache hit is a call you don't pay for. Cache both queries and
  extracted pages.
- **Route by page type.** Cheap/fast extractor for static pages, browser-based only when
  needed, managed API only as last-resort fallback.
- **Self-host primary, managed fallback.** Free path handles the bulk; paid path is a
  safety net so you never fully depend on a flaky self-hosted instance.
- **Async everywhere.** Parallel fetch of top-N pages is what keeps latency competitive
  with managed tools.

## Tool stack

| Layer | Primary (free/self-hosted) | Fallback (managed/paid) | Role |
|-------|----------------------------|-------------------------|------|
| Search | **SearXNG** (Docker) | **Brave Search API** (~$5/1k, own index) | Query → ranked URLs + snippets |
| Extraction (static) | **trafilatura** | — | Fast HTTP-only clean text extraction |
| Extraction (JS-heavy) | **Crawl4AI** (wraps Playwright) | **Firecrawl** (managed) | Render + clean markdown for dynamic pages |
| Rerank/clean (optional) | **Ollama** + small local model | Cheap API tier | Structured extraction / summarization |
| Cache | **Redis** | — | Dedupe queries + cache extracted pages |
| API framework | **FastAPI** + **httpx** + **asyncio** | — | Service layer, concurrency |
| Orchestration | **Docker Compose** | — | Run SearXNG + Redis + service together |

## Why these choices

- **SearXNG** aggregates ~70 engines (Google, Bing, DuckDuckGo, Brave, Wikipedia…) behind
  one JSON API, at $0 marginal cost. You own reliability and rate-limits.
- **Brave** as fallback because it has its own independent index (not a Google/Bing
  reseller), so results stay useful even when SearXNG is blocked.
- **trafilatura** is near-instant for static article text; no browser overhead.
- **Crawl4AI** handles JS rendering and outputs LLM-ready markdown; open source, no
  per-page fee — you pay only infra (~2GB+ RAM for Chromium).
- **Firecrawl** only for the hardest anti-bot pages — pay-per-use, kept out of the hot path.
- **Redis** turns repeated/similar queries (the norm in real apps) into free hits.

## API surface

| Endpoint | Layers used | Use case | Cost profile |
|----------|-------------|----------|--------------|
| `POST /search` | Retrieval | "Just give me links + snippets" | Cheapest |
| `POST /extract` | Extraction | "I have URLs, give me clean text" | Medium |
| `POST /search_and_extract` | Retrieval → Extraction (parallel top-N) | Tavily-shaped combined call | Medium |
| `POST /research` *(optional)* | Retrieval → Extraction → LLM | Structured/summarized answer | Highest, opt-in |

---

# PART 2 — ELABORATE BUILD PLAN

## Phase 0 — Repo & scaffolding

- Initialize repo, Python 3.11+, virtual env / `uv` or `poetry`.
- Add FastAPI, uvicorn, httpx, pydantic, redis, trafilatura, crawl4ai.
- Set up config via pydantic `BaseSettings` reading from env (`.env` for local).
- Add Docker Compose with three services: `api`, `searxng`, `redis`.
- Establish the provider interfaces up front so every later phase plugs into them.

**Suggested directory layout**

```
web-search-service/
├── app/
│   ├── main.py                 # FastAPI app + route wiring
│   ├── config.py               # pydantic settings (env-driven)
│   ├── models.py               # request/response pydantic schemas
│   ├── cache/
│   │   └── redis_cache.py       # get/set with TTL, key normalization
│   ├── search/
│   │   ├── base.py              # SearchProvider interface
│   │   ├── searxng.py           # primary
│   │   └── brave.py             # fallback
│   ├── extract/
│   │   ├── base.py              # ExtractProvider interface
│   │   ├── router.py            # picks extractor by page type
│   │   ├── trafilatura_ext.py   # static
│   │   ├── crawl4ai_ext.py      # JS-heavy
│   │   └── firecrawl_ext.py     # managed fallback
│   ├── rerank/
│   │   └── llm.py               # optional; Ollama or API
│   └── services/
│       ├── search_service.py    # orchestrates search + fallback
│       └── pipeline.py          # search_and_extract, research
├── searxng/
│   └── settings.yml            # JSON enabled, limiter tuned
├── tests/
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

## Phase 1 — Search layer (SearXNG primary)

- Stand up SearXNG via Docker Compose.
- In `searxng/settings.yml`: enable JSON output under `search.formats` (add `- json`),
  and set `server.limiter: false` (or configure proper headers) to avoid 403s on the
  JSON API. Pick a sensible default engine set; disable slow/unreliable ones.
- Implement `SearchProvider` interface: `async def search(query, count, lang, freshness) -> list[SearchResult]`.
- Implement `SearXNGProvider` calling the JSON endpoint via httpx, normalizing results
  into a common `SearchResult` schema (title, url, snippet, engine, score).
- Add `POST /search` returning normalized results.
- **Test:** confirm JSON works, no 403, results parse cleanly.

## Phase 2 — Extraction layer

- Define `ExtractProvider` interface: `async def extract(url) -> ExtractedPage` returning
  `{url, title, markdown, text, status, extractor_used}`.
- Implement `TrafilaturaExtractor` (fetch HTML with httpx, extract main content).
- Implement `Crawl4AIExtractor` for JS-heavy pages (renders, returns markdown).
- Implement a **router** that decides which extractor to use:
  - Try trafilatura first (fast/cheap).
  - If content is empty/too short or page clearly needs JS, escalate to Crawl4AI.
- Add `POST /extract` accepting one or many URLs, fetched concurrently with `asyncio.gather`.
- **Test:** run a static news article and a JS-heavy SPA; verify both return clean text.

## Phase 3 — Combined pipeline

- Implement `search_and_extract`: run `/search`, take top-N URLs, extract them in parallel,
  return merged results (snippet + full markdown per result).
- Make N, timeout, and concurrency configurable per request (with safe defaults).
- Add per-page timeouts so one slow page can't stall the whole response.
- Add `POST /search_and_extract`.
- **Test:** end-to-end latency and result quality vs. a Tavily call on the same query.

## Phase 4 — Caching

- Implement Redis cache wrapper with TTL and normalized keys:
  - Query cache key: normalized query + params (count, lang, freshness).
  - Page cache key: canonicalized URL.
- Wrap search and extract calls: check cache → miss → call provider → store.
- Make TTLs configurable (e.g. news short, static docs long).
- Add a `bypass_cache` flag for callers that need fresh data.
- **Test:** verify second identical request is served from cache and hits no external API.

## Phase 5 — Fallbacks

- Implement `BraveProvider`; wire the search service to fall back to Brave when SearXNG
  returns errors, is rate-limited, or returns too few results.
- Implement `FirecrawlExtractor`; wire the extraction router to escalate to Firecrawl when
  Crawl4AI fails or a page is anti-bot protected.
- Keep both fallbacks behind env flags so they can be disabled entirely for pure-free mode.
- **Test:** simulate SearXNG down → confirm Brave takes over; simulate Crawl4AI failure →
  confirm Firecrawl escalation.

## Phase 6 — Reliability & anti-blocking

- Add retries with backoff (tenacity) around all external calls.
- Add optional proxy rotation for SearXNG's upstream engines and for the extractors.
- Rotate/realistic User-Agents on direct HTTP fetches.
- Add per-provider circuit breakers so a dead provider is skipped fast, not retried forever.
- Add structured logging + request IDs for tracing which layer/provider served a request.

## Phase 7 — Optional LLM layer

- Implement `rerank/llm.py` behind a flag; default provider = Ollama (local, free), with a
  cheap API tier as alternative.
- Add `POST /research`: search → extract top-N → LLM structures/summarizes with citations.
- Strip any hallucinated citation indices that exceed the real source count before returning.
- Keep this strictly opt-in; never invoke the LLM on `/search` or `/extract`.

## Phase 8 — Productionizing

- **Auth:** API-key middleware (per-consumer keys) so your other apps authenticate.
- **Rate limiting:** per-key limits to protect the service and upstream engines.
- **Observability:** request metrics (latency, cache hit rate, provider mix, cost counters),
  health checks per provider.
- **Deploy:** Docker Compose on a VPS to start; document scaling path (separate the browser
  workers from the API when extraction volume grows).
- **Config:** finalize `.env.example` with every provider toggle and TTL documented.

---

## API contracts (draft)

**Request — `/search_and_extract`**
```json
{
  "query": "string",
  "count": 5,
  "extract": true,
  "lang": "en",
  "freshness": "any",
  "bypass_cache": false
}
```

**Response**
```json
{
  "query": "string",
  "results": [
    {
      "title": "string",
      "url": "string",
      "snippet": "string",
      "markdown": "string|null",
      "extractor_used": "trafilatura|crawl4ai|firecrawl|null",
      "from_cache": false
    }
  ],
  "provider": "searxng|brave",
  "took_ms": 0
}
```

## Config / env vars (draft)

```
SEARXNG_URL=http://searxng:8080
BRAVE_API_KEY=            # blank disables Brave fallback
FIRECRAWL_API_KEY=        # blank disables Firecrawl fallback
REDIS_URL=redis://redis:6379/0
CACHE_TTL_SEARCH=3600
CACHE_TTL_PAGE=86400
DEFAULT_RESULT_COUNT=5
MAX_CONCURRENCY=10
PAGE_TIMEOUT_SECONDS=15
ENABLE_LLM_LAYER=false
OLLAMA_URL=http://localhost:11434
SERVICE_API_KEYS=key1,key2   # consumer auth
```

## Cost model

- **Free path** (SearXNG + trafilatura/Crawl4AI + Redis): flat infra cost — one VPS +
  optional proxies — regardless of query volume.
- **Fallback path** (Brave ~$5/1k, Firecrawl per-use): only triggered on failure/edge
  cases, so a small fraction of traffic.
- **LLM path**: only on `/research`, ideally local (Ollama) = $0 marginal.
- Cache hit rate directly reduces every paid call; track it as a first-class metric.

## Testing checklist

- SearXNG JSON returns without 403; results normalize correctly.
- trafilatura on static page; Crawl4AI on JS page; router escalates correctly.
- Parallel extraction respects concurrency + timeouts.
- Cache: identical request served from Redis, zero external calls.
- Fallbacks trigger on simulated primary failure.
- Auth rejects missing/invalid keys; rate limit enforced.
- End-to-end quality + latency benchmarked against Tavily on a fixed query set.

## Open decisions (resolve before/while coding)

1. **Ops tolerance** — self-hosted-first (lowest cost, more babysitting) vs. managed-first
   (Brave/Firecrawl primary, skip SearXNG). Drives which phase is "primary".
2. **Expected query volume** — sets VPS sizing, whether to split browser workers, proxy budget.
3. **Freshness needs** — sets cache TTL strategy per content type.
4. **LLM layer** — needed at launch, or add later? Local vs. API.
5. **Language/runtime** — plan assumes Python (best extractor ecosystem); confirm.
