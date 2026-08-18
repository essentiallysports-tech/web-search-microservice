# Web Search Service — API Reference

Search, extraction, and optional LLM synthesis over one HTTP API. This document is for
teams **consuming** the service. For how it is built and operated, see `README.md`,
`TESTING.md` and `HANDOFF.md`.

---

## Contents

- [Quick start](#quick-start)
- [Authentication](#authentication)
- [Choosing an endpoint](#choosing-an-endpoint)
- [`POST /search`](#post-search)
- [`POST /search_and_extract`](#post-search_and_extract)
- [`POST /extract`](#post-extract)
- [`POST /research`](#post-research)
- [Result statuses](#result-statuses)
- [Errors](#errors)
- [Rate limits](#rate-limits)
- [Caching](#caching)
- [Cost: what you are spending](#cost-what-you-are-spending)
- [Latency: what to expect](#latency-what-to-expect)
- [Operational endpoints](#operational-endpoints)
- [Recipes](#recipes)
- [Gotchas](#gotchas)

---

## Quick start

```bash
curl -X POST https://essentially-search.duckdns.org/search \
  -H "X-API-Key: esw_EXAMPLE_TOKEN_REPLACE_WITH_YOUR_OWN" \
  -H "Content-Type: application/json" \
  -d '{"query": "best CRM 2026", "count": 5}'
```

```json
{
  "query": "best CRM 2026",
  "results": [
    {
      "title": "The 12 best CRM software in 2026",
      "url": "https://zapier.com/blog/best-crm-app/",
      "snippet": "We spent months testing dozens of CRMs…",
      "markdown": null,
      "extractor_used": null,
      "status": "ok",
      "from_cache": false
    }
  ],
  "provider": "serper",
  "cache": "miss",
  "took_ms": 913,
  "request_id": "a4ef7b20a3ce4558"
}
```

---

## Authentication

Every request needs an `X-API-Key` header:

```
X-API-Key: esw_EXAMPLE_TOKEN_REPLACE_WITH_YOUR_OWN
```

**To get one**, ask the service owner (rajat@essentiallysports.com) for a token, and say
which app it is for — tokens are issued per-app, not per-person. You will be sent the
secret once.

Notes that matter:

- **One token per app.** Each gets its own rate-limit budget, so another team's runaway
  loop cannot throttle you, and your token can be revoked without touching anyone else.
- **The secret is shown once.** Only a hash is stored, so it cannot be recovered. If you
  lose it, ask for a new one — the old one gets revoked.
- **Tokens may carry an expiry.** Ask which yours has. An expired token returns `401`
  exactly like a wrong one, so build for that.
- **Treat it like a password.** Environment variable or secret manager, never in
  client-side code, a mobile app, or a git commit. Anyone holding it can spend money
  against your budget.

Missing or invalid key:

```json
{ "detail": "missing or invalid API key" }
```
`HTTP 401` · header `WWW-Authenticate: ApiKey`

---

## Choosing an endpoint

This is the decision that determines your bill. The service deliberately separates three
jobs that managed alternatives bundle into one price.

| You need | Use | Relative cost |
|---|---|---|
| Ranked URLs and snippets | `POST /search` | 1× |
| Clean text from URLs you already have | `POST /extract` | 3× |
| Search **and** full page text | `POST /search_and_extract` | 4× |
| A written, cited answer | `POST /research` | 10× |

**Start with `/search`.** Snippets answer more than people expect, and it is the only
endpoint that never fetches a page. Reach for `/search_and_extract` when you genuinely
need article bodies — feeding an LLM, building a summary, extracting structured facts.

Do **not** call `/search` and then `/extract` on the results. `/search_and_extract` does
that in one round trip, pre-filters URLs that cannot be crawled, and bounds the total
time. Two calls costs more and gives you less.

---

## `POST /search`

Ranked results and snippets. Never fetches a page, never calls an LLM.

### Request

Every field except `query` is optional. Omitting one applies the **deployment's**
default, not a value hardcoded in the API — so the numbers below are what this
deployment currently does, and another could be configured differently. Ask if you need
to depend on one exactly; otherwise send the field explicitly.

| Field | Type | Default | Notes |
|---|---|---|---|
| `query` | string | **required** | 1–500 chars. Trimmed; blank is rejected. |
| `count` | int | `10` | 1–20. Ask for 10 even if you need 3 — see [cost](#cost-what-you-are-spending). |
| `lang` | string | `"en"` | Language hint, max 8 chars. |
| `freshness` | enum | `"any"` | `any` · `day` · `week` · `month` · `year`. |
| `bypass_cache` | bool | `false` | **Costs money every time.** See [caching](#caching). |

### Response

| Field | Type | Notes |
|---|---|---|
| `query` | string | Echoed back. |
| `results` | array | See below. |
| `provider` | string | `serper` normally, `brave` if the primary failed. |
| `cache` | enum | `hit` · `miss` · `bypass` · `coalesced`. |
| `took_ms` | int | Server-side duration. |
| `request_id` | string | Quote this when reporting a problem. |

Each result:

| Field | Type | Notes |
|---|---|---|
| `title` | string | |
| `url` | string | |
| `snippet` | string | Provider's preview text. |
| `markdown` | null | Always null here — this endpoint does not extract. |
| `extractor_used` | null | Always null here. |
| `status` | string | Always `"ok"`; there is no extraction to report. |
| `from_cache` | bool | Whether the result set came from cache. |

### Example

```bash
curl -X POST https://essentially-search.duckdns.org/search \
  -H "X-API-Key: $SEARCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "redis persistence options", "count": 10, "freshness": "year"}'
```

---

## `POST /search_and_extract`

Search, then fetch and clean the top results. The main endpoint for LLM pipelines.

### Request

Everything from `/search`, plus:

| Field | Type | Default | Notes |
|---|---|---|---|
| `extract` | bool | `true` | `false` makes this behave like `/search`. |
| `extract_top_k` | int | `5` | 0–20. **Your main cost lever.** How many of `count` results get fetched. `0` extracts nothing. |
| `max_tier` | enum | `firecrawl` | `trafilatura` · `http_retry` · `firecrawl`. Set `http_retry` to guarantee zero extraction spend. |
| `extract_deadline_s` | float | `25.0` | 0–120. Total budget for the fetch fan-out. Pages that miss it degrade to snippet-only. |

### Response

Everything from `/search`, plus:

| Field | Type | Notes |
|---|---|---|
| `extracted` | int | How many results carry `markdown`. |
| `attempted` | int | How many were tried. |

Results now carry `markdown`, `extractor_used`, and a meaningful `status`.

### Example

```bash
curl -X POST https://essentially-search.duckdns.org/search_and_extract \
  -H "X-API-Key: $SEARCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is a CDN", "count": 5, "extract_top_k": 3}'
```

```json
{
  "query": "what is a CDN",
  "results": [
    { "url": "https://www.cloudflare.com/learning/cdn/what-is-a-cdn/",
      "status": "ok", "extractor_used": "firecrawl", "markdown": "# What is a CDN?…" },
    { "url": "https://www.reddit.com/r/explainlikeimfive/…",
      "status": "skipped", "extractor_used": null, "markdown": null },
    { "url": "https://en.wikipedia.org/wiki/Content_delivery_network",
      "status": "ok", "extractor_used": "trafilatura", "markdown": "…" },
    { "url": "https://aws.amazon.com/what-is/cdn/",
      "status": "ok", "extractor_used": "trafilatura", "markdown": "…" },
    { "url": "https://www.akamai.com/glossary/what-is-a-cdn",
      "status": "not_attempted", "extractor_used": null, "markdown": null }
  ],
  "provider": "serper",
  "cache": "miss",
  "extracted": 3,
  "attempted": 3,
  "took_ms": 3667
}
```

Read that response carefully — it shows the three ways a result can lack `markdown`:

- **`skipped`** — policy refused it. Reddit disallows crawling in `robots.txt`, so the
  slot was spent on the next candidate instead of wasted.
- **`not_attempted`** — ranked 5th with `extract_top_k: 3`, so nothing was tried.
- Neither is a failure. `attempted: 3` and `extracted: 3` means everything tried worked.

---

## `POST /extract`

Clean text from URLs you already have. No search.

### Request

| Field | Type | Default | Notes |
|---|---|---|---|
| `urls` | array | **required** | 1–20 absolute http/https URLs. |
| `max_tier` | enum | `firecrawl` | As above. `http_retry` never bills. |
| `timeout_s` | float | per-tier | 0–60, per page. Clamped to the batch budget. |
| `bypass_cache` | bool | `false` | Re-fetches pages already cached for 24h. |

### Response

| Field | Type | Notes |
|---|---|---|
| `results` | array | Same order as `urls`, duplicates included. |
| `took_ms` | int | |
| `request_id` | string | |

Results here have an empty `snippet` — there is no search result to draw one from.

Bounded to **60 seconds** total. 20 URLs at a fan-out of 5 is four waves, so a full batch
of slow pages can take most of that.

### Example

```bash
curl -X POST https://essentially-search.duckdns.org/extract \
  -H "X-API-Key: $SEARCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://en.wikipedia.org/wiki/Redis"], "max_tier": "http_retry"}'
```

---

## `POST /research`

Search → extract → a cited answer from an LLM. The most expensive endpoint.

**Opt-in.** Returns `503` unless the deployment enabled the LLM layer.

### Request

Everything from `/search_and_extract`, plus:

| Field | Type | Default | Notes |
|---|---|---|---|
| `instruction` | string | none | Free-form steer, e.g. `"answer as three bullet points"`. |

`extract` is ignored — synthesis over snippets alone is not worth paying for.

### Response

Everything from `/search`, plus:

| Field | Type | Notes |
|---|---|---|
| `answer` | string | Markdown, with inline `[1]` `[2]` citation markers. |
| `citations` | array | URLs actually used, in citation order. |
| `model` | string | The model that answered. |

### Example

```bash
curl -X POST https://essentially-search.duckdns.org/research \
  -H "X-API-Key: $SEARCH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is Redis used for", "count": 5, "extract_top_k": 3,
       "instruction": "answer in three bullet points"}'
```

```json
{
  "answer": "Redis is used for:\n\n- **Caching** — …[1]\n- **Message brokering** — …[2]",
  "citations": [
    "https://redis.io/tutorials/what-is-redis/",
    "https://www.geeksforgeeks.org/system-design/introduction-to-redis-server/"
  ],
  "model": "anthropic/claude-haiku-4.5",
  "took_ms": 6120
}
```

Citations are validated against the real source list, so a hallucinated index is dropped
rather than returned as a link to nothing. An answer with an empty `citations` array
means the model could not ground it — treat that as a weak answer.

`422` if results were found but none could be extracted — there was nothing to reason
over. Retry with a higher `extract_top_k`, or fall back to `/search`.

---

## Result statuses

`status` on each result tells you what happened to **that** result's extraction.

| Status | Meaning | Retry? |
|---|---|---|
| `ok` | Usable content in `markdown`. | — |
| `not_attempted` | Ranked below `extract_top_k`. Nothing was tried. | Raise `extract_top_k`. |
| `skipped` | Policy refused: `robots.txt`, or not HTML (PDF, image, video). | No. Stable. |
| `empty` | Fetched, but no main content found. | Rarely helps. |
| `blocked` | Anti-bot wall. | Sometimes later. |
| `timeout` | Host did not answer in time. | Yes. |
| `error` | Transport failure — DNS, refused, dead host. | No. |
| `unavailable` | The service's own extractor was temporarily circuit-broken. | **Yes, immediately.** |

Two that are easy to get wrong:

**`ok` always means content.** It is never set for a result nobody tried, so
`status == "ok"` is a safe filter for "this has `markdown`".

**`unavailable` is about us, not your URL.** It means an internal breaker was open, so
nothing was learned about the page. It is not cached and an immediate retry is reasonable.

---

## Errors

All errors are JSON with a `detail` field.

| Status | Meaning | What to do |
|---|---|---|
| `401` | Missing, wrong, revoked, or expired key. | Check the header. Ask for a new token. |
| `403` | Valid credential, insufficient privilege. | You used an issued token on an admin route. |
| `422` | Request failed validation, or `/research` found no extractable sources. | Read `detail` — it names the field. |
| `429` | Rate limit exceeded. | Honour `Retry-After`. |
| `502` | Every search provider failed. | Retry with backoff. `detail.attempts` says what was tried. |
| `503` | LLM layer disabled, or synthesis failed. | Not retryable for the disabled case. |

`422` names the offending field, including valid values for enums:

```json
{ "detail": [ { "type": "enum", "loc": ["body", "max_tier"],
  "msg": "Input should be 'trafilatura', 'http_retry' or 'firecrawl'",
  "input": "crawl4ai" } ] }
```

Every response carries `X-Request-ID`, echoed as `request_id` in the body. **Log it** —
it is how a problem gets traced server-side.

---

## Rate limits

A budget of **cost units per minute per token**, not a flat request count. A cached
`/search` and an LLM call are not equivalent, so they are not counted equivalently.

| Endpoint | Units | Max/min at a 120-unit budget |
|---|---|---|
| `/search` | 1 | 120 |
| `/extract` | 3 | 40 |
| `/search_and_extract` | 4 | 30 |
| `/research` | 10 | 12 |

Every response carries:

```
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 116
```

On `429`:

```
Retry-After: 37
```

```json
{ "detail": { "error": "rate limit exceeded",
  "limit_per_minute": 120, "retry_after_s": 37 } }
```

**Honour `Retry-After`.** It is a fixed window, so the budget resets whole rather than
trickling back — retrying sooner just burns another rejection. Budgets are per token, so
your limit is yours alone.

---

## Caching

Two layers, transparent to you. Read `cache` on the response:

| Value | Meaning |
|---|---|
| `hit` | Served from cache. No upstream call, ~20ms. |
| `miss` | Fetched fresh and cached for next time. |
| `coalesced` | An identical request was already in flight; you got its result. |
| `bypass` | You set `bypass_cache`. |

TTLs: search results 1 hour (5 min for `freshness`-constrained), extracted pages 24
hours, failures 30 minutes.

Cached search keys normalize the query — casefolded, whitespace collapsed, stopwords
dropped, tokens sorted. So `"best CRM 2026"`, `"Best CRM 2026?"` and `"best crm  2026"`
share one entry. One consequence: queries differing **only in word order** collapse
together, so `"dog bites man"` and `"man bites dog"` may return the same results.

`count` is part of the key, so a 5-result request cannot serve from a cached 10-result
one. **Pick one `count` and slice locally** for the best hit rate.

### About `bypass_cache`

It skips reading the cache and buys a fresh result every single time.

Reserve it for cases where staleness is genuinely unacceptable. A loop with
`bypass_cache: true` takes the cache hit rate from 79% to 0% and roughly **5× the cost
per request**. If you find yourself setting it by default, the TTL is the wrong knob to
be fighting — ask for it to be lowered instead.

---

## Cost: what you are spending

Real money per call, so a little care here goes a long way.

**1. `extract_top_k` is the biggest lever.** Extraction dominates cost once search is a
flat ~$0.001. `extract_top_k: 10` costs roughly double `5` **and** adds the slowest of
ten pages to your latency. Most tasks are well served by 3–5.

**2. Ask for 10 results even if you need 3.** The search provider charges one credit for
up to 10 results and two above it. `count: 3` costs the same as `count: 10`; `count: 20`
costs double. So 10 is free headroom, and 20 should be deliberate.

**3. `max_tier: "http_retry"` guarantees zero extraction spend.** Two free tiers handle
about 67% of pages. The rest come back `blocked` or `empty` instead of being fetched by
the paid scraper — a real trade, not a degraded mode. Good for bulk or
best-effort work.

**4. Cache hits are free.** Consistent `count` and query phrasing across your app is the
cheapest optimisation available.

**5. `/research` spends tokens.** Roughly 8,700 input / 340 output per call. Use
`extract_top_k` to control how much text is fed in.

---

## Latency: what to expect

| Call | Typical | Slow case |
|---|---|---|
| `/search` cached | ~11ms | |
| `/search` fresh | ~950ms | ~2.3s |
| `/search_and_extract` cached | ~20ms | |
| `/search_and_extract` fresh | ~3–9s | ~20s |
| `/research` | ~6s | ~25s |

Fresh combined calls are slow because they fetch real pages, and some need a rendering
service that takes seconds. Practical advice:

- **Set client timeouts to at least 30s** for `/search_and_extract` and `/research`.
  Under that you will cancel requests that were about to succeed.
- **Warm calls are ~100× faster.** If latency matters, consistent queries help more than
  any parameter.
- **Lower `extract_deadline_s`** if you would rather have snippets quickly than markdown
  eventually. Pages that miss it degrade rather than failing.
- **A client timeout does not refund anything.** Upstream calls already dispatched are
  billed whether or not you are still waiting, so aggressive client-side timeouts plus
  retries cost more than one patient request. Retrying a slow call is the most expensive
  thing you can do here.

---

## Operational endpoints

Both are unauthenticated and safe to poll.

### `GET /health`

```json
{ "status": "ok",
  "providers": { "serper": "ok", "brave": "ok", "cache": "ok",
                 "trafilatura": "ok", "http_retry": "ok", "firecrawl": "ok" },
  "version": "0.1.0" }
```

`status` is `ok` · `degraded` · `down`, computed from **search providers only** — a
degraded cache makes the service more expensive, not unavailable. Returns `503` when
`down`, so it works as a load-balancer readiness probe.

### `GET /livez`

Returns `ok` as plain text whenever the process is alive. Use for liveness; it stays
`200` during an upstream outage the service can recover from.

---

## Recipes

### Feed an LLM with source text

```json
{ "query": "kubernetes ingress vs gateway api", "count": 10, "extract_top_k": 4 }
```

Then use only `status == "ok"` results, and cite `url`.

### Bulk enrichment with a hard zero-spend guarantee

```json
{ "urls": ["https://…", "https://…"], "max_tier": "http_retry" }
```

Expect ~67% success. Re-queue `blocked` and `empty` for a later pass at full tier if the
content is worth paying for.

### Fast, cheap "is there anything about X"

```json
{ "query": "…", "count": 10, "extract": false }
```

Snippets only, 1 unit, and usually a cache hit.

### Recent news

```json
{ "query": "…", "freshness": "week", "count": 10 }
```

Note the shorter 5-minute cache TTL — freshness queries are cheaper to keep current but
cost more in misses.

### Retrying correctly

```python
import time, httpx

def search(client, payload, token, attempts=3):
    for attempt in range(attempts):
        r = client.post("/search", json=payload, headers={"X-API-Key": token})
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30)))
            continue
        if r.status_code in (502, 503) and attempt < attempts - 1:
            time.sleep(2 ** attempt)          # backoff, these are upstream failures
            continue
        r.raise_for_status()                  # 401/422 are your bug, not a blip
        return r.json()
    raise RuntimeError("search unavailable")
```

Retry `429` (after `Retry-After`), `502` and `503`. Never retry `401`, `403` or `422` —
they will fail identically and just burn budget.

---

## Gotchas

**A result with `status: "ok"` and `markdown: null` cannot happen.** If you see it,
report it — `ok` is only ever set from a real extraction.

**`not_attempted` is not an error.** It means your `extract_top_k` was lower than the
result count. Very common and usually intended.

**`extract_top_k` counts *extractable* results, not raw rank.** URLs refused by
`robots.txt` are filtered out before slots are spent, so a budget of 3 gives you 3
attempts rather than 3 top-ranked URLs of which some were never crawlable.

**Duplicate URLs in `/extract` are collapsed.** You get one result per input position,
but the page is fetched once.

**PDFs are not extracted.** They come back `skipped` by the content-type gate, without
being downloaded.

**`count` participates in the cache key.** Varying it per call quietly destroys your hit
rate.

**Word order is not preserved in cache keys.** See [caching](#caching).

**`max_tier` is a ceiling, not a target.** `firecrawl` does not mean "use the paid
scraper" — it means "you may, if the free tiers fail". Most pages never reach it.

**Your token may expire.** Expiry returns `401`, identical to a wrong key. Handle `401`
as "get a new credential", not "retry".
