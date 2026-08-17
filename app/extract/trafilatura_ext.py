"""Tier 0 — trafilatura over a plain HTTP fetch.

The cheapest path by a wide margin, and where most article content resolves.

trafilatura is synchronous and CPU-bound, so it runs in a worker thread: inline it
would block the event loop for every parse, which with a gather over N pages
serializes the whole batch.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import trafilatura

from app.common.useragents import pick_profile
from app.config import Settings
from app.extract.base import ExtractProvider
from app.extract.fetch import fetch_html
from app.models import ExtractedPage, ExtractorName

# Module-level: never varies per request, and rebuilding it per call is allocation
# on a hot path.
_EXTRACT_OPTS: dict[str, Any] = {
    "output_format": "markdown",
    "include_links": True,
    "include_tables": True,
    "include_formatting": True,
    "include_comments": False,  # comment sections are noise for LLM consumption
    "include_images": False,
    "favor_precision": True,  # prefer clean text over recall of boilerplate
    "deduplicate": True,
}


class TrafilaturaExtractor(ExtractProvider):
    name = ExtractorName.TRAFILATURA
    billable = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(settings, client)

    async def _extract(self, url: str, *, timeout_s: float) -> ExtractedPage:
        # Even the cheap tier rotates its UA — a fixed one is the easiest crawler
        # fingerprint to block.
        profile = pick_profile(rotate=self.settings.rotate_user_agents)

        result = await fetch_html(
            self.client,
            url,
            timeout_s=timeout_s,
            max_bytes=self.settings.max_html_bytes,
            headers={"User-Agent": profile.user_agent},
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


async def extract_from_html(
    url: str, html: str, *, final_url: str = ""
) -> ExtractedPage:
    """Run trafilatura over already-fetched HTML, off the event loop.

    Shared by both free tiers so a caller cannot tell which one served a page from
    the output format.

    The worker thread buys concurrency with the FETCHES, not parallelism across
    parses — trafilatura holds the GIL through most of its work, so simultaneous
    parses largely serialize. Parses per request is therefore a latency decision as
    well as a cost one.
    """
    markdown, text, title = await asyncio.to_thread(_extract_sync, html, url)

    # `markdown` alone decides this. `text` is not a fallback: it is only ever set
    # when markdown already is.
    if not markdown:
        return ExtractedPage(
            url=url, final_url=final_url or None, status="empty", error="no main content found"
        )

    return ExtractedPage(
        url=url,
        final_url=final_url or None,
        title=title,
        markdown=markdown,
        text=text,
        status="ok",
    )


def _extract_sync(html: str, url: str) -> tuple[str | None, str | None, str | None]:
    """One parse per page. Exactly one — a test asserts the count.

    Do not add a second `trafilatura.extract` call to derive a plain-text field. It
    is not cheap: parsing is this service's dominant cost at 2.3-4.9s per large page
    against 125-1030ms to fetch it, and the second pass measured 44% of extraction
    CPU. Removing it took a five-page parallel extraction from 14.9s to 4.6s.
    """
    markdown = trafilatura.extract(html, url=url, **_EXTRACT_OPTS)

    title: str | None = None
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata is not None:
            title = metadata.title
    except Exception:
        # Metadata is a bonus; never let it fail the extraction.
        title = None

    # `text` stays on the model because the paid tier fills it for free (it already
    # holds one markdown string) and the cached envelope carries the field.
    return markdown, None, title


def _map_status(fetch_status: str) -> str:
    return {
        "blocked": "blocked",
        "timeout": "timeout",
        "empty": "empty",
        "unsupported": "skipped",
    }.get(fetch_status, "error")
