"""Shared, size-capped HTTP fetching for the free extraction tiers.

Two protections for the worker:

- Streaming with a byte cap, so a 200MB response is aborted mid-flight rather than
  buffered into memory first.
- Content-type gating, so a PDF or video is refused before download instead of being
  handed to an HTML extractor for a guaranteed empty result.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)

# Body signals meaning "bot wall", not "content". Detecting these is what lets the
# router escalate instead of returning a useless 200.
_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies to continue",
    "cf-browser-verification",
    "px-captcha",
    "/cdn-cgi/challenge-platform",
    "captcha-delivery.com",
    "access denied",
    "attention required!",
)

_HTML_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")

_BLOCK_STATUS = frozenset({401, 403, 405, 406, 409, 429, 451})


@dataclass(slots=True)
class FetchResult:
    status: str  # ok | blocked | timeout | error | unsupported | empty
    html: str = ""
    final_url: str = ""
    status_code: int | None = None
    content_type: str = ""
    error: str | None = None
    truncated: bool = False


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch a page as text, bounded by `max_bytes`."""
    try:
        async with client.stream(
            "GET", url, timeout=timeout_s, headers=headers or {}
        ) as response:
            content_type = response.headers.get("content-type", "").lower()

            if response.status_code in _BLOCK_STATUS:
                return FetchResult(
                    status="blocked",
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    error=f"HTTP {response.status_code}",
                )
            if response.status_code >= 400:
                return FetchResult(
                    status="error",
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    error=f"HTTP {response.status_code}",
                )

            if content_type and not any(t in content_type for t in _HTML_TYPES):
                # Refuse before the body: don't download 50MB to learn it isn't HTML.
                return FetchResult(
                    status="unsupported",
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    error=f"unsupported content-type: {content_type}",
                )

            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    truncated = True
                    break

            body = b"".join(chunks)[:max_bytes]
            html = body.decode(response.encoding or "utf-8", errors="replace")

            if _looks_blocked(html):
                return FetchResult(
                    status="blocked",
                    html=html,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    error="anti-bot interstitial",
                    truncated=truncated,
                )

            return FetchResult(
                status="ok" if html.strip() else "empty",
                html=html,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=content_type,
                truncated=truncated,
            )

    except httpx.TimeoutException:
        return FetchResult(status="timeout", error=f"timeout after {timeout_s}s")
    except httpx.TooManyRedirects as exc:
        return FetchResult(status="error", error=f"redirect loop: {exc}")
    except httpx.HTTPError as exc:
        return FetchResult(status="error", error=f"transport error: {exc!r}")


def _looks_blocked(html: str) -> bool:
    # Head only. Challenge pages are short by nature, and scanning the whole document
    # would be slower AND prone to false positives from article text that happens to
    # discuss CAPTCHAs.
    head = html[:4096].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)
