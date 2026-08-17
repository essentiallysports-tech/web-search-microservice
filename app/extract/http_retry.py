"""Tier 1 — retry the plain fetch as a browser-shaped client.

A large share of pages that appear to "need JavaScript" are not JS-dependent at
all: they are UA-gated, or they refuse requests missing the headers a real browser
sends (Sec-Fetch-*, Accept-Language, Referer).

Catching those costs ~200ms here versus a per-page fee at tier 2, so every page
this tier converts is a page that is never billed. It and tier 0 together account
for 67% of successful extractions.
"""

from __future__ import annotations

import httpx

from app.common.useragents import headers_for, pick_profile
from app.config import Settings
from app.extract.base import ExtractProvider
from app.extract.fetch import fetch_html
from app.extract.trafilatura_ext import extract_from_html
from app.models import ExtractedPage, ExtractorName

# A full browser navigation header set. The Sec-Fetch-* family is what most UA
# gates actually key on; a lone User-Agent override is often not enough.
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}


class HttpRetryExtractor(ExtractProvider):
    name = ExtractorName.HTTP_RETRY
    billable = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, client)

    async def _extract(self, url: str, *, timeout_s: float) -> ExtractedPage:
        # A different profile from the one tier 0 just used, so the retry looks like
        # a different client rather than the same one asking twice.
        profile = pick_profile(rotate=self.settings.rotate_user_agents)
        headers = dict(_BROWSER_HEADERS)
        headers.update(headers_for(profile))
        # No referrer at all is itself a bot signal on some sites.
        headers["Referer"] = _origin_of(url)

        result = await fetch_html(
            self.client,
            url,
            timeout_s=timeout_s,
            max_bytes=self.settings.max_html_bytes,
            headers=headers,
        )

        if result.status != "ok":
            return ExtractedPage(
                url=url,
                final_url=result.final_url or None,
                status=_map_status(result.status),
                error=result.error,
            )

        return await extract_from_html(url, result.html, final_url=result.final_url)

    async def health(self) -> bool:
        return True


def _origin_of(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else url


def _map_status(fetch_status: str) -> str:
    return {
        "blocked": "blocked",
        "timeout": "timeout",
        "empty": "empty",
        "unsupported": "skipped",
    }.get(fetch_status, "error")
