"""Reddit search — via redditapis.com, not Apify.

Official Reddit commercial API access is gated behind a ~$12,000/month minimum
with mandatory pre-approval (confirmed live, Sept 2026 research). Apify's Reddit
actors are per-item cheap on the store page (~$1.29-2/1k) but the real bill for
this org's EXISTING Apify usage runs to ~$1,000/month once platform/compute/
storage overhead is counted — the same "headline price isn't the real price"
trap `HANDOFF.md` already documents for this service's own search/extract
providers. redditapis.com is a flat, pay-per-call reseller with no
subscription and no platform-rental fee: $0.002/call, confirmed via its own
docs, which is the property that matters here, not just the number on the
pricing page.

Confidence note: this provider's request/response shape was fetched directly
from redditapis.com's own OpenAPI spec and docs pages (auth header, base URL,
endpoint, query params, and response field names all confirmed), unlike
twitter_api.py's — see that module's own confidence note. Still worth a live
smoke test against a real key before trusting in production; nothing here has
been exercised against the real API.
"""

from __future__ import annotations

from datetime import UTC, datetime
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

# redditapis.com's own time-window codes, closest match to this service's Freshness enum.
_TIMEFRAME: dict[Freshness, str] = {
    Freshness.DAY: "day",
    Freshness.WEEK: "week",
    Freshness.MONTH: "month",
    Freshness.YEAR: "year",
}

_MAX_COUNT = 100  # documented API limit


class RedditApiProvider(SearchProvider):
    name = SearchProviderName.REDDITAPIS
    billable = True
    _COST_PER_CALL_USD = 0.002  # flat, confirmed in the provider's own docs

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        budget: Budget | None = None,
    ) -> None:
        super().__init__(settings, client, budget)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.redditapis_api_key)

    def estimate_cost_usd(self, results: list[SearchResult]) -> float:
        return self._COST_PER_CALL_USD

    async def _search(
        self,
        query: str,
        *,
        count: int,
        lang: str,
        freshness: Freshness,
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": query,
            "limit": min(count, _MAX_COUNT),
            "sort": "relevance",
        }
        if t := _TIMEFRAME.get(freshness):
            params["t"] = t

        payload = await self._fetch(params)
        return self._normalize(payload)[:count]

    async def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.get(
                self.settings.redditapis_endpoint,
                params=params,
                timeout=self.settings.search_timeout_s,
                headers={"Authorization": f"Bearer {self.settings.redditapis_api_key}"},
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
                f"HTTP {response.status_code} — check REDDITAPIS_API_KEY or account balance",
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
        # Field names per redditapis.com's published OpenAPI spec. Tries both
        # `posts` (documented) and `results`/`data` (common alternate shapes on
        # similar services) so a minor spec drift degrades to "fewer results"
        # rather than "silently empty" — but this is unverified against a real
        # response; correct the primary key first if live testing shows different.
        entries = payload.get("posts") or payload.get("results") or payload.get("data") or []
        if not isinstance(entries, list):
            return []

        seen: set[str] = set()
        out: list[SearchResult] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or item.get("permalink") or "").strip()
            if url.startswith("/"):
                url = f"https://www.reddit.com{url}"
            if not url.startswith(("http://", "https://")):
                continue

            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)

            title = (item.get("title") or "").strip()
            body = (item.get("selftext") or "").strip()
            subreddit = item.get("subreddit")
            author = item.get("author")
            snippet_bits = [b for b in (body[:300], f"r/{subreddit}" if subreddit else None,
                                          f"u/{author}" if author else None) if b]

            created = item.get("created_utc") or item.get("created")
            out.append(
                SearchResult(
                    title=title or (body[:120] if body else url),
                    url=url,
                    snippet=" · ".join(snippet_bits),
                    engine="reddit",
                    score=float(item["score"]) if isinstance(item.get("score"), (int, float)) else None,
                    published_at=self._published_at(created),
                )
            )
        return out

    @staticmethod
    def _published_at(created: object) -> str | None:
        """Reddit's own `created_utc` is a Unix epoch number, not a string —
        `to_iso8601` (built for Serper/Brave's string-formatted dates) silently
        returns None for anything that isn't a str, which would drop every
        Reddit result's date without ever raising. Handled directly here
        instead of reusing that helper for this field."""
        if isinstance(created, bool):
            return None
        if isinstance(created, (int, float)):
            try:
                return (
                    datetime.fromtimestamp(created, tz=UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(created, str):
            return to_iso8601(created)
        return None

    async def health(self) -> bool:
        # A probe query would spend $0.002 with no free status endpoint documented.
        # Configuration is all that's checkable without paying.
        return self.enabled
