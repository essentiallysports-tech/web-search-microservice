"""Tier escalation — where cost per extraction is decided.

Walks the ladder cheapest-first and stops on usable content, so only pages that
defeated the free tiers reach the paid one.

    0  trafilatura   free   ~100ms   most article pages end here
    1  http_retry    free   ~200ms   UA-gated pages
    2  firecrawl     PAID   ~2-13s   JS-dependent pages and anti-bot walls

A result is accepted when it is `ok` and at least MIN_EXTRACT_CHARS long.
Everything else escalates, except `skipped` (policy) and a caller's `max_tier`.
Short-but-ok results are kept as a fallback — a little real text beats nothing.

Two guards keep the paid tier from being billed pointlessly; both are load-bearing
and both have their reasoning at the point of use below.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.common.hostlimit import HostLimiter
from app.common.metrics import extract_escalations, extract_rescues
from app.config import Settings
from app.extract.base import TIER_ORDER, ExtractProvider
from app.extract.robots import RobotsPolicy
from app.logging_setup import get_logger
from app.models import ExtractedPage, ExtractorName

log = get_logger(__name__)

#: Statuses where trying a more capable extractor is pointless. Only policy
#: decisions belong here — robots.txt and non-HTML are properties of the URL that
#: no amount of extra capability changes.
#:
#: An open circuit breaker must NOT land here. It once did (reported as `skipped`),
#: which meant one tripped tier bypassed the whole ladder and the empty result was
#: negative-cached for 30 minutes. It reports `unavailable` now.
_TERMINAL = frozenset({"skipped"})

#: Failures that justify spending money. `blocked` means anti-bot, which the
#: scraper's own egress gets through; `empty` means the content needed rendering.
#:
#: Deliberately excludes `error`/`timeout` (the URL never answered — a scraper
#: resolves the same DNS and fails identically) and `unavailable` (our breaker was
#: open, so we learned nothing about the URL and have no grounds to pay).
_WORTH_ESCALATING = frozenset({"blocked", "empty"})


@dataclass(slots=True)
class ExtractionAttempt:
    extractor: ExtractorName
    status: str
    chars: int = 0


@dataclass(slots=True)
class RoutedExtraction:
    page: ExtractedPage
    attempts: list[ExtractionAttempt] = field(default_factory=list)

    @property
    def tiers_used(self) -> int:
        return len(self.attempts)


class ExtractRouter:
    def __init__(
        self,
        settings: Settings,
        extractors: list[ExtractProvider],
        robots: RobotsPolicy | None = None,
        host_limiter: HostLimiter | None = None,
    ) -> None:
        self.settings = settings
        # Sorted by cost, and an unconfigured tier (no Firecrawl key) is dropped
        # once here rather than re-checked on every request.
        self.extractors = sorted(
            (e for e in extractors if e.enabled), key=lambda e: e.tier
        )
        self.robots = robots
        # Wraps every tier, so escalating doesn't multiply load on the host that
        # just refused us.
        self.host_limiter = host_limiter or HostLimiter(
            concurrency=settings.per_host_concurrency,
            min_delay_s=settings.per_host_delay_s,
        )
        log.info(
            "extract_router.ready",
            tiers=[str(e.name) for e in self.extractors],
            per_host_concurrency=settings.per_host_concurrency,
        )

    async def startup(self) -> None:
        for extractor in self.extractors:
            await extractor.startup()

    async def shutdown(self) -> None:
        for extractor in self.extractors:
            await extractor.shutdown()

    async def extract(
        self,
        url: str,
        *,
        max_tier: ExtractorName = ExtractorName.FIRECRAWL,
        timeout_s: float | None = None,
        deadline_at: float | None = None,
    ) -> RoutedExtraction:
        """Walk the ladder for one URL.

        `deadline_at` is a `time.monotonic()` instant after which the caller stops
        waiting (the batch deadline from `ExtractService.extract_many`). Advisory for
        free tiers, load-bearing for paid ones — see the budget check below.
        """
        if self.robots is not None and not await self.robots.allows(url):
            log.info("extract.robots_disallowed", url=url)
            return RoutedExtraction(
                page=ExtractedPage(url=url, status="skipped", error="disallowed by robots.txt"),
                attempts=[],
            )

        ceiling = TIER_ORDER[max_tier]
        attempts: list[ExtractionAttempt] = []
        best_partial: ExtractedPage | None = None
        last: ExtractedPage | None = None

        for extractor in self.extractors:
            if extractor.tier > ceiling:
                break

            # Guard 1: never pay to rediscover that a URL is unreachable. If every
            # cheaper tier failed at the transport level rather than being blocked
            # or empty, a scraper resolves the same DNS and fails the same way.
            if extractor.billable and attempts and not any(
                a.status in _WORTH_ESCALATING for a in attempts
            ):
                log.info(
                    "extract.skipping_paid_tier",
                    url=url,
                    extractor=str(extractor.name),
                    reason="no recoverable failure seen",
                    seen=[a.status for a in attempts],
                )
                extract_escalations.labels(str(extractor.name), "skipped_unreachable").inc()
                break

            # Guard 2: never start a billable call the caller will not wait for.
            # `extract_many` cancels pending tasks at the deadline, but cancelling an
            # in-flight scrape does not un-bill it — the page was charged and the
            # result thrown away. Fires on pages that arrive here having already
            # spent the free tiers, which is exactly when it matters.
            #
            # Reserve `wall_clock_s`, NOT the nominal timeout: an extractor may grant
            # itself transport slack, and under-reserving is how a billed scrape gets
            # cancelled anyway. Firecrawl's slack was an undeclared +10s for a while.
            #
            # A sustained `skipped_no_time` count means the reverse failure —
            # FIRECRAWL_TIMEOUT_S crowding EXTRACT_DEADLINE_S until the tier is
            # unreachable. That shipped once and looked like "pages are empty".
            if extractor.billable and deadline_at is not None:
                needed = extractor.wall_clock_s(self._timeout_for(extractor, timeout_s))
                remaining = deadline_at - time.monotonic()
                if remaining < needed:
                    log.info(
                        "extract.skipping_paid_tier",
                        url=url,
                        extractor=str(extractor.name),
                        reason="insufficient time budget",
                        remaining_s=round(remaining, 1),
                        needed_s=round(needed, 1),
                    )
                    extract_escalations.labels(
                        str(extractor.name), "skipped_no_time"
                    ).inc()
                    break

            async with self.host_limiter.slot(url):
                page = await extractor.extract(
                    url, timeout_s=self._timeout_for(extractor, timeout_s)
                )
            attempts.append(
                ExtractionAttempt(extractor.name, page.status, page.char_count)
            )
            last = page

            if page.status == "ok" and page.char_count >= self.settings.min_extract_chars:
                if len(attempts) > 1:
                    extract_escalations.labels(str(extractor.name), "resolved").inc()
                    # Record what this tier rescued the page FROM, not just that it
                    # won. `empty` is a rendering win and IP-independent; `blocked`
                    # is a bot wall beaten from a different egress, and a rising
                    # share of those means our address is being scored worse.
                    extract_rescues.labels(
                        str(extractor.name), attempts[-2].status
                    ).inc()
                return RoutedExtraction(page=page, attempts=attempts)

            # Thin but real content — worth keeping in case nothing better turns up.
            if page.status == "ok" and (
                best_partial is None or page.char_count > best_partial.char_count
            ):
                best_partial = page

            if page.status in _TERMINAL:
                return RoutedExtraction(page=page, attempts=attempts)

            if extractor.tier < ceiling:
                extract_escalations.labels(str(extractor.name), page.status).inc()
                log.debug(
                    "extract.escalating",
                    url=url,
                    from_tier=str(extractor.name),
                    status=page.status,
                    chars=page.char_count,
                )

        if best_partial is not None:
            # Short content from a working extractor beats a hard failure.
            return RoutedExtraction(page=best_partial, attempts=attempts)

        if last is not None:
            return RoutedExtraction(page=last, attempts=attempts)

        return RoutedExtraction(
            page=ExtractedPage(url=url, status="error", error="no extractor available"),
            attempts=attempts,
        )

    def _timeout_for(self, extractor: ExtractProvider, override: float | None) -> float:
        if override is not None:
            return override
        # Keyed per-extractor, NOT by a `tier >= N` comparison. That form was written
        # when a browser was the top tier and silently handed Firecrawl the browser's
        # 25s — larger than the whole batch deadline, so the paid tier was unreachable
        # by construction. The wrong SOURCE, not the wrong number, was the defect.
        if extractor.name is ExtractorName.FIRECRAWL:
            return self.settings.firecrawl_timeout_s
        return self.settings.page_timeout_s

    async def health(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        for extractor in self.extractors:
            try:
                alive = await extractor.health()
            except Exception:
                alive = False
            statuses[str(extractor.name)] = "ok" if alive else "down"
        return statuses
