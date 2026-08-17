"""Tier 2 — Firecrawl (managed, paid).

The only tier that costs money per page, so it is the last resort: reached only
after both free tiers fail, and disabled entirely by leaving FIRECRAWL_API_KEY
blank or capping `max_tier`.

It is also what makes blocking survivable. Firecrawl scrapes from its own egress,
so a page this host's IP cannot fetch costs ~$0.00083 instead of failing — which is
why the paid tier is not really optional on a datacenter address.

Every call increments `wss_external_calls_total{billable="true"}`, the spend meter.
"""

from __future__ import annotations

import httpx
import orjson

from app.config import Settings
from app.extract.base import ExtractProvider
from app.logging_setup import get_logger
from app.models import ExtractedPage, ExtractorName

log = get_logger(__name__)

#: Transport slack on top of the budget we ask Firecrawl for, covering the two
#: network legs and a queued start so a call isn't killed locally just as the answer
#: arrives.
#:
#: Small and declared on purpose. As an undeclared `+ 10.0` it let a scrape run 25s
#: against the router's 15s reservation — billed, then discarded by the batch
#: deadline. `wall_clock_s` is what keeps the two numbers in agreement.
_TRANSPORT_MARGIN_S = 2.0


class FirecrawlExtractor(ExtractProvider):
    name = ExtractorName.FIRECRAWL
    billable = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, client)

    @property
    def enabled(self) -> bool:
        # No key means the paid tier simply does not exist — pure-free mode.
        return bool(self.settings.firecrawl_api_key)

    def wall_clock_s(self, timeout_s: float) -> float:
        return timeout_s + _TRANSPORT_MARGIN_S

    async def _extract(self, url: str, *, timeout_s: float) -> ExtractedPage:
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "timeout": int(timeout_s * 1000),
        }

        try:
            response = await self.client.post(
                self.settings.firecrawl_endpoint,
                json=payload,
                # Must equal wall_clock_s(), or the router's reservation is wrong.
                timeout=self.wall_clock_s(timeout_s),
                headers={
                    "Authorization": f"Bearer {self.settings.firecrawl_api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException:
            return ExtractedPage(url=url, status="timeout", error=f"firecrawl timeout after {timeout_s}s")
        except httpx.HTTPError as exc:
            return ExtractedPage(url=url, status="error", error=f"firecrawl transport error: {exc!r}")

        if response.status_code in (401, 403):
            log.error("firecrawl.auth_failed", status=response.status_code)
            return ExtractedPage(url=url, status="error", error="firecrawl auth failed — check FIRECRAWL_API_KEY")
        if response.status_code == 402:
            log.error("firecrawl.quota_exhausted")
            return ExtractedPage(url=url, status="error", error="firecrawl quota exhausted")
        if response.status_code == 429:
            return ExtractedPage(url=url, status="blocked", error="firecrawl rate limited")
        if response.status_code >= 400:
            return ExtractedPage(url=url, status="error", error=f"firecrawl HTTP {response.status_code}")

        try:
            body = orjson.loads(response.content)
        except orjson.JSONDecodeError as exc:
            return ExtractedPage(url=url, status="error", error=f"firecrawl malformed JSON: {exc}")

        data = body.get("data") or {}
        markdown = (data.get("markdown") or "").strip()
        if not markdown:
            return ExtractedPage(url=url, status="empty", error="firecrawl returned no markdown")

        metadata = data.get("metadata") or {}
        return ExtractedPage(
            url=url,
            final_url=metadata.get("sourceURL") or metadata.get("url") or None,
            title=metadata.get("title") or None,
            markdown=markdown,
            text=markdown,
            status="ok",
        )

    async def health(self) -> bool:
        # No cheap health endpoint worth burning quota on; configuration is the
        # only thing we can check without spending money.
        return self.enabled
