"""ExtractProvider interface.

Extractors are ordered tiers, cheapest first. `tier` is what the router walks and
what a caller's `max_tier` clamps, so cost per request is bounded by the caller.

  0  trafilatura   plain HTTP + main-content extraction. Milliseconds, free.
  1  http_retry    same, with a realistic UA and HTTP/2. Catches pages that are
                   UA-gated rather than genuinely JS-dependent.
  2  firecrawl     managed, per-page fee. JS-dependent and anti-bot pages.

No local-browser tier, deliberately — one existed and was removed because its wins
depended on our egress IP rather than the renderer. PROGRESS.md Phase 12.

An extractor never raises for a page-level problem: it returns a non-"ok"
`ExtractedPage` so the router can decide whether escalating is worth it.
Exceptions are reserved for the extractor itself being broken.
"""

from __future__ import annotations

import abc
import time

import httpx

from app.common.circuit import CircuitBreaker
from app.common.metrics import extract_attempts, external_calls, provider_duration
from app.config import Settings
from app.http_client import get_extraction_client
from app.models import ExtractedPage, ExtractorName

TIER_ORDER: dict[ExtractorName, int] = {
    ExtractorName.TRAFILATURA: 0,
    ExtractorName.HTTP_RETRY: 1,
    ExtractorName.FIRECRAWL: 2,
}


class ExtractProvider(abc.ABC):
    """One extraction strategy."""

    name: ExtractorName
    billable: bool = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self.breaker = CircuitBreaker(
            str(self.name),
            fail_threshold=settings.circuit_fail_threshold,
            reset_after_s=settings.circuit_reset_after_s,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """The extraction client (optionally proxied), unless one was injected."""
        return self._client if self._client is not None else get_extraction_client()

    @property
    def tier(self) -> int:
        return TIER_ORDER[self.name]

    @property
    def enabled(self) -> bool:
        return True

    def wall_clock_s(self, timeout_s: float) -> float:
        """Worst-case real time one attempt can take.

        The router's paid-tier guard reserves this before starting a billable call,
        so it must be the true ceiling. An extractor granting itself transport slack
        on top of `timeout_s` has to say so here, or the guard under-reserves and the
        batch deadline cancels a scrape that has already been billed.
        """
        return timeout_s

    # ---------------------------------------------------------------- contract

    @abc.abstractmethod
    async def _extract(self, url: str, *, timeout_s: float) -> ExtractedPage:
        """Attempt extraction. Return a non-ok `ExtractedPage` on page failure."""

    async def startup(self) -> None:
        """Optional warm-up hook. No-op for every shipped tier — they are stateless
        HTTP — but kept so a future stateful provider has somewhere to go."""

    async def shutdown(self) -> None:
        """Optional teardown hook."""

    # ------------------------------------------------------------- public path

    async def extract(self, url: str, *, timeout_s: float | None = None) -> ExtractedPage:
        timeout = timeout_s or self.settings.page_timeout_s

        if not self.breaker.allows():
            # `unavailable`, NOT `skipped`. The router treats `skipped` as terminal,
            # so reporting an open breaker that way made one tripped tier bypass the
            # whole ladder and negative-cache the empty result for 30 minutes. This
            # status means "ask the next tier" and claims nothing about the URL.
            extract_attempts.labels(str(self.name), "unavailable").inc()
            return ExtractedPage(
                url=url,
                status="unavailable",
                extractor_used=self.name,
                error="circuit_open",
            )

        started = time.perf_counter()
        try:
            page = await self._extract(url, timeout_s=timeout)
        except Exception as exc:
            await self.breaker.record_failure()
            extract_attempts.labels(str(self.name), "error").inc()
            return ExtractedPage(
                url=url, status="error", extractor_used=self.name, error=repr(exc)
            )
        finally:
            provider_duration.labels(str(self.name)).observe(time.perf_counter() - started)
            external_calls.labels(str(self.name), str(self.billable).lower()).inc()

        page.extractor_used = self.name
        page.fetched_at = time.time()
        page.char_count = len(page.markdown or page.text or "")

        # A page-level failure counts against the breaker only when it looks like
        # the extractor is at fault; "empty" usually means the page, not us.
        if page.status in {"error", "timeout"}:
            await self.breaker.record_failure()
        else:
            await self.breaker.record_success()

        extract_attempts.labels(str(self.name), page.status).inc()
        return page
