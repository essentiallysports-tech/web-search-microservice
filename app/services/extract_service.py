"""Extraction orchestration: caching, negative caching, bounded concurrency.

Three cost controls:

- Page cache by canonical URL. The same article surfaces across many queries, so
  extracting it once is the difference between one paid scrape and twenty.
- Negative cache, so a failing URL doesn't re-pay the whole ladder every request.
  Only failures that are properties of the URL are stored — see `_record_failure`.
- Two-level concurrency: a per-request fan-out cap so one caller can't monopolise
  the worker, and a process-wide semaphore so N requests can't multiply into
  N x fan-out outbound fetches.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.cache.keys import failure_key, page_key
from app.cache.layer import CacheLayer
from app.common.metrics import extract_tiers_used
from app.config import Settings
from app.extract.router import ExtractRouter
from app.logging_setup import get_logger
from app.models import CacheState, ExtractedPage, ExtractorName

log = get_logger(__name__)

_ENVELOPE_VERSION = 1


@dataclass(slots=True)
class ExtractionResult:
    page: ExtractedPage
    cache: CacheState = CacheState.MISS
    tiers_used: int = 0


class ExtractService:
    def __init__(
        self,
        settings: Settings,
        router: ExtractRouter,
        cache: CacheLayer | None = None,
    ) -> None:
        self.settings = settings
        self.router = router
        self.cache = cache
        self._global_limit = asyncio.Semaphore(max(1, settings.max_concurrency))

    # ------------------------------------------------------------------ many

    async def extract_many(
        self,
        urls: list[str],
        *,
        max_tier: ExtractorName = ExtractorName.FIRECRAWL,
        timeout_s: float | None = None,
        bypass_cache: bool = False,
        deadline_s: float | None = None,
    ) -> list[ExtractionResult]:
        """Extract URLs concurrently, preserving input order.

        Duplicates within one request are collapsed — the same page is never
        extracted twice for one caller.

        `deadline_s` bounds the whole batch: pages still running when it expires are
        cancelled and returned as `timeout`, so one pathological page degrades a
        single result rather than the response.
        """
        per_request = asyncio.Semaphore(max(1, self.settings.extract_concurrency))
        unique: dict[str, str] = {}
        for url in urls:
            unique.setdefault(page_key(url, version=self.settings.cache_version), url)

        # Handed down so the router can refuse to START a billable tier it cannot
        # finish: cancelling an in-flight paid scrape does not refund it.
        deadline_at = (
            time.monotonic() + deadline_s if deadline_s is not None else None
        )

        async def run(url: str) -> ExtractionResult:
            async with per_request, self._global_limit:
                return await self.extract_one(
                    url,
                    max_tier=max_tier,
                    timeout_s=timeout_s,
                    bypass_cache=bypass_cache,
                    deadline_at=deadline_at,
                )

        tasks: dict[asyncio.Task, str] = {
            asyncio.create_task(run(url)): key for key, url in unique.items()
        }

        done, pending = await asyncio.wait(tasks.keys(), timeout=deadline_s)
        for task in pending:
            task.cancel()
        if pending:
            # Let cancellation land before returning, so no task outlives its request.
            await asyncio.gather(*pending, return_exceptions=True)
            log.info("extract.deadline_exceeded", pending=len(pending), deadline_s=deadline_s)

        by_key: dict[str, ExtractionResult] = {}
        for task, key in tasks.items():
            url = unique[key]
            if task in pending:
                by_key[key] = ExtractionResult(
                    page=ExtractedPage(
                        url=url, status="timeout", error=f"batch deadline {deadline_s}s exceeded"
                    )
                )
                continue
            error = task.exception()
            if error is not None:
                log.error("extract.unhandled", url=url, error=repr(error))
                by_key[key] = ExtractionResult(
                    page=ExtractedPage(url=url, status="error", error=repr(error))
                )
            else:
                by_key[key] = task.result()

        # Re-expand to the caller's original list, duplicates included.
        return [by_key[page_key(u, version=self.settings.cache_version)] for u in urls]

    # ------------------------------------------------------------------- one

    async def extract_one(
        self,
        url: str,
        *,
        max_tier: ExtractorName = ExtractorName.FIRECRAWL,
        timeout_s: float | None = None,
        bypass_cache: bool = False,
        deadline_at: float | None = None,
    ) -> ExtractionResult:
        if self.cache is None or not self.cache.enabled:
            routed = await self.router.extract(
                url, max_tier=max_tier, timeout_s=timeout_s, deadline_at=deadline_at
            )
            extract_tiers_used.observe(routed.tiers_used)
            return ExtractionResult(
                page=routed.page, cache=CacheState.BYPASS, tiers_used=routed.tiers_used
            )

        if not bypass_cache:
            known_failure = await self._check_negative_cache(url)
            if known_failure is not None:
                return ExtractionResult(page=known_failure, cache=CacheState.HIT)

        key = page_key(url, version=self.settings.cache_version)
        tiers = 0

        async def compute() -> dict[str, Any]:
            nonlocal tiers
            routed = await self.router.extract(
                url, max_tier=max_tier, timeout_s=timeout_s, deadline_at=deadline_at
            )
            tiers = routed.tiers_used
            extract_tiers_used.observe(tiers)

            if routed.page.status != "ok":
                await self._record_failure(url, routed.page)
            return _to_envelope(routed.page)

        envelope, state = await self.cache.get_or_compute(
            key,
            compute,
            ttl=lambda env: self._ttl_for(env),
            namespace="page",
            bypass=bypass_cache,
        )
        return ExtractionResult(
            page=_from_envelope(envelope), cache=state, tiers_used=tiers
        )

    # ------------------------------------------------------------- internals

    def _ttl_for(self, envelope: dict[str, Any]) -> int:
        # Only successes occupy the page cache. Failures go to the negative cache
        # with a much shorter TTL, so a transient outage can't pin a bad result for
        # a day.
        return self.settings.cache_ttl_page if envelope.get("status") == "ok" else 0

    async def _check_negative_cache(self, url: str) -> ExtractedPage | None:
        if self.cache is None:
            return None

        envelope = await self.cache.get(
            failure_key(url, version=self.settings.cache_version),
            namespace="page_failure",
        )
        if not envelope:
            return None

        log.debug("extract.negative_cache_hit", url=url, status=envelope.get("status"))
        page = _from_envelope(envelope)
        page.error = f"{page.error or 'previous attempt failed'} (cached failure)"
        return page

    async def _record_failure(self, url: str, page: ExtractedPage) -> None:
        if self.cache is None or self.settings.cache_ttl_failure <= 0:
            return
        # Only cache failures that are properties of the URL. `unavailable` means our
        # breakers were open, so caching it would turn a transient local fault into
        # 30 minutes of empty answers per URL after the breakers had closed.
        # `skipped` (robots, non-HTML) IS stable, so that one is worth remembering.
        if page.status == "unavailable":
            log.debug("extract.not_caching_local_failure", url=url)
            return
        await self.cache.set(
            failure_key(url, version=self.settings.cache_version),
            _to_envelope(page),
            ttl=self.settings.cache_ttl_failure,
            namespace="page_failure",
        )

    async def allows(self, url: str) -> bool:
        """Whether policy permits extracting this URL, without fetching it.

        The router re-checks before each attempt; asking first is what lets the
        pipeline spend a slot on the next candidate rather than on a URL that was
        always going to be refused. Cheap in steady state — robots decisions are
        memoized per process and cached per origin for 24h.
        """
        robots = self.router.robots
        if robots is None:
            return True
        return await robots.allows(url)

    async def health(self) -> dict[str, str]:
        return await self.router.health()


# ----------------------------------------------------------------- helpers


def _to_envelope(page: ExtractedPage) -> dict[str, Any]:
    return {"v": _ENVELOPE_VERSION, **page.model_dump(mode="json")}


def _from_envelope(envelope: dict[str, Any]) -> ExtractedPage:
    data = {k: v for k, v in envelope.items() if k != "v"}
    return ExtractedPage.model_validate(data)
