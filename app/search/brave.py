"""Brave Search — the fallback.

Here because it runs its OWN index rather than reselling Google. That independence is
the entire justification, since it is otherwise the dearer option (~$5/1k against
Serper's ~$1/1k): a fallback that resold the same upstream would share a failure mode
with the primary, i.e. insurance that lapses exactly when you claim on it.

Brave includes $5/month of free credit (~1,000 queries) and this only fires when
Serper errors or under-returns, so in practice the insurance is free. Past that
allowance, a rising Brave share is worth alerting on rather than absorbing — watch
`wss_search_provider_calls_total`.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson

from app.cache.keys import canonical_url
from app.config import Settings
from app.logging_setup import get_logger
from app.models import Freshness, SearchProviderName, SearchResult
from app.search.base import SearchProvider, SearchProviderError
from app.search.dates import to_iso8601

log = get_logger(__name__)

# Brave's recency codes: past day/week/month/year.
_FRESHNESS: dict[Freshness, str] = {
    Freshness.DAY: "pd",
    Freshness.WEEK: "pw",
    Freshness.MONTH: "pm",
    Freshness.YEAR: "py",
}

_MAX_COUNT = 20  # API hard limit


class BraveProvider(SearchProvider):
    name = SearchProviderName.BRAVE
    billable = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, client)

    @property
    def enabled(self) -> bool:
        # No key means the fallback does not exist — pure-free mode.
        return bool(self.settings.brave_api_key)

    def overfetch(self, count: int) -> int:
        """Brave has no `-site:` operator, so exclusion here is post-hoc only and
        the dropped slots cannot be backfilled by the index. Over-fetching is the
        whole remedy — and it is free, because Brave bills per CALL rather than by
        depth. `prepare_query` is deliberately left as the no-op default.
        """
        return min(count * 2, _MAX_COUNT)

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
            "count": min(count, _MAX_COUNT),
            "safesearch": "off",
            "text_decorations": "false",  # strip <strong> markup from snippets
            "extra_snippets": "false",
        }
        if lang:
            params["search_lang"] = lang
        if code := _FRESHNESS.get(freshness):
            params["freshness"] = code

        payload = await self._fetch(params)
        return self._normalize(payload)[:count]

    async def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.get(
                self.settings.brave_endpoint,
                params=params,
                timeout=self.settings.search_timeout_s,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.settings.brave_api_key,
                },
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
            # Config errors, not transient — retrying achieves nothing.
            log.error("brave.auth_failed", status=response.status_code)
            raise SearchProviderError(
                str(self.name),
                f"HTTP {response.status_code} — check BRAVE_API_KEY",
                retryable=False,
            )
        if response.status_code == 422:
            raise SearchProviderError(
                str(self.name), "422 — rejected query parameters", retryable=False
            )
        if response.status_code == 429:
            # Brave's free tier caps at 1 query/second as well as 2k/month, so this is
            # a normal condition rather than an outage.
            raise SearchProviderError(str(self.name), "429 — rate limited or quota exhausted")
        if response.status_code >= 400:
            raise SearchProviderError(
                str(self.name),
                f"HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )

    def _normalize(self, payload: dict[str, Any]) -> list[SearchResult]:
        entries = ((payload.get("web") or {}).get("results")) or []

        seen: set[str] = set()
        results: list[SearchResult] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            url = (item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue

            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                SearchResult(
                    title=(item.get("title") or "").strip(),
                    # Brave calls the snippet "description".
                    snippet=(item.get("description") or "").strip(),
                    url=url,
                    engine="brave",
                    # No relevance score is exposed; None beats inventing one.
                    score=None,
                    # `page_age` is ISO, `age` is prose ("2 days ago"). Both leave
                    # here as ISO so the field means one thing across providers.
                    published_at=to_iso8601(item.get("page_age") or item.get("age")),
                )
            )
        return results

    async def health(self) -> bool:
        # Brave has no free health endpoint, and a probe query would consume
        # quota. Configuration is the only thing checkable without spending.
        return self.enabled
