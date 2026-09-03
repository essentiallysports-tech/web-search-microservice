"""Twitter/X search — via twitterapi.io, not Apify, not the official X API.

Official X API pay-per-use pricing (Feb 2026 rollout) is $0.005/read, capped at
2M reads/month before forcing an Enterprise plan (~$42K/month) — confirmed live
research, Sept 2026. Apify's tweet-scraper actors are $0.18-0.40/1k on their
store pages, but this org's real Apify bill for existing, unrelated usage runs
~$1,000/month once platform/compute/storage overhead is counted — see
reddit_api.py's module docstring for the same reasoning applied there.
twitterapi.io is $0.15/1k tweets, pay-as-you-go, no minimum spend, credits
that never expire — a dedicated flat-rate reseller, same shape as
redditapis.com, deliberately chosen over Apify for the same reason.

CONFIDENCE NOTE — read before trusting this in production:

The auth header (`x-api-key`) is confirmed directly from twitterapi.io's own
docs. The exact base URL and endpoint path below are a best-effort
reconstruction from the docs site's navigation and general knowledge of this
specific service, NOT a directly-fetched, verified spec — the live fetch for
the full OpenAPI definition 404'd, and the docs page itself didn't expose the
request/response shape without one. This is a materially different confidence
level from reddit_api.py's provider, which WAS fetched and confirmed end to
end.

Do not deploy this against real traffic without first hitting the endpoint
with a real API key and confirming the response shape matches `_normalize`
below — a wrong guess here fails LOUDLY (SearchProviderError on any
unexpected shape, never silently) by design, so the first real call will
either work or tell you exactly what to fix in this file.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson

from app.cache.keys import canonical_url
from app.common.budget import Budget
from app.config import Settings
from app.logging_setup import get_logger
from app.models import Freshness, SearchProviderName, SearchResult
from app.search.base import SearchProvider, SearchProviderError
from app.search.dates import to_iso8601

log = get_logger(__name__)


class TwitterApiProvider(SearchProvider):
    name = SearchProviderName.TWITTERAPI
    billable = True
    #: $0.15 per 1,000 tweets — i.e. per RESULT, not per call, unlike Reddit's
    #: flat per-call price. Charged per result actually returned, in `_search`,
    #: not via the generic `estimate_cost_usd` hook (which only sees the final
    #: post-exclusion-filter list — fine here since this provider has no
    #: `prepare_query`/domain-exclusion behavior to interact with it).
    _COST_PER_TWEET_USD = 0.00015

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        budget: Budget | None = None,
    ) -> None:
        super().__init__(settings, client, budget)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.twitterapi_api_key)

    def estimate_cost_usd(self, results: list[SearchResult]) -> float:
        return len(results) * self._COST_PER_TWEET_USD

    async def _search(
        self,
        query: str,
        *,
        count: int,
        lang: str,
        freshness: Freshness,
    ) -> list[SearchResult]:
        # Best-effort mapping onto Twitter's advanced-search query syntax
        # (documented at github.com/igorbrigadir/twitter-advanced-search, the
        # same reference apidojo/tweet-scraper's own Apify listing points to).
        # `lang:` and lightweight recency hints fold into the query string
        # itself rather than separate params, since the advanced-search
        # grammar is query-embedded, not a set of sibling fields.
        q = query
        if lang:
            q = f"{q} lang:{lang}"

        params: dict[str, Any] = {"query": q, "queryType": "Latest"}
        payload = await self._fetch(params)
        return self._normalize(payload)[:count]

    async def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.get(
                self.settings.twitterapi_endpoint,
                params=params,
                timeout=self.settings.search_timeout_s,
                headers={"x-api-key": self.settings.twitterapi_api_key},
            )
        except httpx.TimeoutException as exc:
            raise SearchProviderError(
                str(self.name), f"timeout after {self.settings.search_timeout_s}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(str(self.name), f"transport error: {exc!r}") from exc

        self._raise_for_status(response)

        try:
            payload = orjson.loads(response.content)
        except orjson.JSONDecodeError as exc:
            raise SearchProviderError(str(self.name), f"malformed JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SearchProviderError(str(self.name), "unexpected JSON shape (not an object)")
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise SearchProviderError(
                str(self.name),
                f"HTTP {response.status_code} — check TWITTERAPI_API_KEY or account balance",
                retryable=False,
            )
        if response.status_code == 404:
            # Most likely diagnosis for THIS provider specifically, given the
            # confidence note above: the endpoint path guess is wrong, not
            # that the resource doesn't exist. Says so rather than reading as
            # a generic 4xx.
            raise SearchProviderError(
                str(self.name),
                "404 — verify TWITTERAPI_ENDPOINT against twitterapi.io's real API "
                "reference; the path in .env.example is an unverified best guess",
                retryable=False,
            )
        if response.status_code == 429:
            raise SearchProviderError(str(self.name), "429 — rate limited")
        if response.status_code >= 400:
            raise SearchProviderError(
                str(self.name),
                f"HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )

    def _normalize(self, payload: dict[str, Any]) -> list[SearchResult]:
        # Field names are a best-effort guess at a REST wrapper over Twitter's
        # own v2 API shape (which this class of reseller typically mirrors
        # closely) — `tweets` list, each with `text`/`url`/`author`/`createdAt`.
        # Tries a couple of plausible alternate keys so a near-miss degrades to
        # fewer results rather than silently zero, but confirm against a real
        # response before relying on this — see module confidence note.
        entries = payload.get("tweets") or payload.get("data") or payload.get("results") or []
        if not isinstance(entries, list):
            return []

        seen: set[str] = set()
        out: list[SearchResult] = []
        for item in entries:
            if not isinstance(item, dict):
                continue

            url = (item.get("url") or item.get("twitterUrl") or "").strip()
            tweet_id = item.get("id") or item.get("id_str")
            author = item.get("author") if isinstance(item.get("author"), dict) else {}
            handle = author.get("userName") or author.get("username") or item.get("authorHandle")
            if not url and tweet_id and handle:
                url = f"https://twitter.com/{handle}/status/{tweet_id}"
            if not url.startswith(("http://", "https://")):
                continue

            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)

            text = (item.get("text") or item.get("fullText") or "").strip()
            created = item.get("createdAt") or item.get("created_at")

            out.append(
                SearchResult(
                    title=text[:120] if text else url,
                    url=url,
                    snippet=text,
                    engine="twitter",
                    score=None,
                    published_at=to_iso8601(created) if isinstance(created, str) else None,
                )
            )
        return out

    async def health(self) -> bool:
        # A probe call would spend real credits with no documented free status
        # endpoint. Configuration is all that's checkable without paying.
        return self.enabled
