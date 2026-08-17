"""Serper — the primary retrieval path.

Resells Google's SERP as JSON, which is why it is first: the quality problem this
service spent a phase fighting was the INDEX, not the retrieval code. It is also the
cheapest option by a wide margin (~$1/1k, ~$0.30/1k at volume, against $5/1k for
Brave), which is unusual enough to be worth stating.

Two cost details that are easy to get wrong:

- `num > 10` costs TWO credits. Serper prices by result depth, so asking for 20
  results doubles the per-query cost. `MAX_FREE_DEPTH` makes the boundary visible
  here rather than on the invoice.
- Credits are prepaid and expire six months after purchase, so buying ahead of
  measured volume is not a saving — and a deployment can start 401ing with no
  config change at all when a balance runs out.

Reports Serper's own `credits` figure into `wss_search_credits_total`, which is more
trustworthy than counting calls: it already accounts for the deep-result surcharge.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson

from app.cache.keys import canonical_url
from app.common.metrics import search_credits_used
from app.config import Settings
from app.logging_setup import get_logger
from app.models import Freshness, SearchProviderName, SearchResult
from app.search.base import SearchProvider, SearchProviderError

log = get_logger(__name__)

# Google's `tbs` recency codes, which Serper passes straight through.
_TIME_RANGE: dict[Freshness, str] = {
    Freshness.DAY: "qdr:d",
    Freshness.WEEK: "qdr:w",
    Freshness.MONTH: "qdr:m",
    Freshness.YEAR: "qdr:y",
}

#: Result depth included in a single credit. Above this Serper charges double.
MAX_FREE_DEPTH = 10

_MAX_COUNT = 100  # API hard limit on `num`


class SerperProvider(SearchProvider):
    name = SearchProviderName.SERPER
    billable = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, client)

    @property
    def enabled(self) -> bool:
        # No key means no search at all now this is the primary; startup validation
        # catches it rather than booting a service that 502s every request.
        return bool(self.settings.serper_api_key)

    async def _search(
        self,
        query: str,
        *,
        count: int,
        lang: str,
        freshness: Freshness,
    ) -> list[SearchResult]:
        num = min(count, _MAX_COUNT)
        if num > MAX_FREE_DEPTH:
            # Not an error — just where cost per query doubles, which is worth seeing
            # in logs rather than only on the invoice.
            log.info("serper.deep_result_set", requested=num, credits=2)

        body: dict[str, Any] = {
            "q": query,
            "num": num,
            "gl": self.settings.serper_country,
        }
        if lang:
            body["hl"] = lang
        if tbs := _TIME_RANGE.get(freshness):
            body["tbs"] = tbs

        payload = await self._fetch(body)
        self._record_credits(payload)
        return self._normalize(payload)[:count]

    async def _fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.settings.serper_endpoint,
                json=body,
                timeout=self.settings.search_timeout_s,
                headers={
                    "X-API-KEY": self.settings.serper_api_key,
                    "Content-Type": "application/json",
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
            # Serper answers BOTH a bad key and an exhausted prepaid balance in this
            # range, and neither is fixed by retrying. Naming both in the error saves
            # the "why is search down" investigation.
            log.error("serper.auth_or_quota_failed", status=response.status_code)
            raise SearchProviderError(
                str(self.name),
                f"HTTP {response.status_code} — check SERPER_API_KEY, or the prepaid "
                "credit balance (credits expire six months after purchase)",
                retryable=False,
            )
        if response.status_code == 400:
            raise SearchProviderError(
                str(self.name), "400 — rejected query parameters", retryable=False
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
        entries = payload.get("organic") or []

        seen: set[str] = set()
        results: list[SearchResult] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            # Serper names the URL field `link`, not `url`.
            url = (item.get("link") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue

            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)

            results.append(
                SearchResult(
                    title=(item.get("title") or "").strip(),
                    url=url,
                    snippet=(item.get("snippet") or "").strip(),
                    engine="google",
                    # `position` is a rank, not a score; converting would invent
                    # precision. The list is already in rank order.
                    score=None,
                    published_at=item.get("date") or None,
                )
            )
        return results

    def _record_credits(self, payload: dict[str, Any]) -> None:
        """Record the vendor's own credit figure.

        Better than counting calls: it already accounts for the double charge on deep
        result sets, so the meter cannot silently understate the bill.
        """
        credits = payload.get("credits")
        if isinstance(credits, bool) or not isinstance(credits, (int, float)):
            return
        if credits > 0:
            search_credits_used.labels(str(self.name)).inc(credits)

    async def health(self) -> bool:
        # A probe query would spend a credit, and there is no free status
        # endpoint. Configuration is all that is checkable without paying.
        return self.enabled
