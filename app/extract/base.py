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

from app.common.budget import Budget, BudgetExceededError, BudgetUnavailableError
from app.common.circuit import CircuitBreaker
from app.common.metrics import extract_attempts, external_calls, provider_duration
from app.config import Settings
from app.http_client import get_extraction_client
from app.logging_setup import get_logger
from app.models import ExtractedPage, ExtractorName

log = get_logger(__name__)

TIER_ORDER: dict[ExtractorName, int] = {
    ExtractorName.TRAFILATURA: 0,
    ExtractorName.HTTP_RETRY: 1,
    ExtractorName.FIRECRAWL: 2,
}


class ExtractProvider(abc.ABC):
    """One extraction strategy."""

    name: ExtractorName
    billable: bool = False
    #: What one successful extraction is believed to cost. Only read for
    #: billable tiers. A flat estimate is the right shape here (unlike search,
    #: no extractor reports its own per-call price the way Serper reports
    #: credits), so this is a plain class attribute a billable subclass sets.
    estimated_cost_usd: float = 0.0

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self.budget = budget
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

        # Budget gate, same shape and same "fails closed" reasoning as the search
        # side (app/search/base.py). `unavailable` here too, for the identical
        # reason: a refused-for-budget page is not evidence about the URL, so the
        # router must not negative-cache it or treat it as a terminal failure.
        if self.billable and self.budget is not None:
            try:
                await self.budget.check()
            except (BudgetExceededError, BudgetUnavailableError) as exc:
                extract_attempts.labels(str(self.name), "budget_refused").inc()
                log.error("extract.budget_refused", extractor=str(self.name), error=str(exc))
                return ExtractedPage(
                    url=url, status="unavailable", extractor_used=self.name, error=str(exc)
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

        # Charged whenever the tier actually ran (status != unavailable, which
        # returned early above and never reached here) — a billable tier bills
        # for the attempt, not just a clean `ok`. Matches this class's own
        # `external_calls.labels(..., billable=...)` counter above, which also
        # increments on every real attempt regardless of outcome.
        if self.billable and self.budget is not None and self.estimated_cost_usd > 0:
            await self.budget.charge(str(self.name), self.estimated_cost_usd)

        extract_attempts.labels(str(self.name), page.status).inc()
        return page
