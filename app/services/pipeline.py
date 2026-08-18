"""Combined search → extract pipeline — the Tavily-shaped call.

Deliberately NOT cached as a unit. The search result and each page are already
cached separately, and page entries are shared across every query that surfaces the
same URL; a composite cache would duplicate that storage and discard the whole entry
whenever any one component expired.

Degradation is graceful throughout: a page that fails, is blocked, or misses the
deadline comes back snippet-only rather than failing the request. The search call
has already been paid for, so a snippet is still worth returning.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.logging_setup import get_logger
from app.models import (
    CacheState,
    ExtractorName,
    Freshness,
    ResultItem,
    SearchProviderName,
)
from app.services.extract_service import ExtractService
from app.services.search_service import SearchService

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineOutcome:
    query: str
    items: list[ResultItem]
    provider: SearchProviderName | None = None
    search_cache: CacheState = CacheState.MISS
    degraded: bool = False
    extracted_ok: int = 0
    extracted_attempted: int = 0
    pages_from_cache: int = 0
    took_ms: int = 0
    attempted: list[str] = field(default_factory=list)


class SearchExtractPipeline:
    def __init__(
        self,
        settings: Settings,
        search: SearchService,
        extract: ExtractService,
    ) -> None:
        self.settings = settings
        self.search = search
        self.extract = extract

    async def run(
        self,
        query: str,
        *,
        count: int,
        lang: str = "en",
        freshness: Freshness = Freshness.ANY,
        extract: bool = True,
        #: None means DEFAULT_EXTRACT_TOP_K; 0 means extract nothing.
        extract_top_k: int | None = None,
        max_tier: ExtractorName = ExtractorName.FIRECRAWL,
        bypass_cache: bool = False,
        deadline_s: float | None = None,
        #: None means SEARCH_BLOCKED_DOMAINS; an empty set disables filtering.
        exclude: frozenset[str] | None = None,
    ) -> PipelineOutcome:
        started = time.perf_counter()

        outcome = await self.search.search(
            query,
            count=count,
            lang=lang,
            freshness=freshness,
            bypass_cache=bypass_cache,
            exclude=exclude,
        )

        # `not_attempted` up front, overwritten per result once extraction runs.
        # The model default is "ok", which on an unextracted result is a lie a
        # caller cannot detect: `status="ok"` with `markdown=null` reads exactly
        # like "we extracted it and the page was empty". Every path out of this
        # method therefore leaves a status that means what it says — including the
        # early returns below, where nothing is extracted at all.
        items = [
            ResultItem(
                title=r.title,
                url=r.url,
                snippet=r.snippet,
                status="not_attempted",
                published_at=r.published_at,
            )
            for r in outcome.results
        ]
        result = PipelineOutcome(
            query=query,
            items=items,
            provider=outcome.provider,
            search_cache=outcome.cache,
            degraded=outcome.degraded,
            attempted=outcome.attempted,
        )

        if not extract or not items:
            result.took_ms = int((time.perf_counter() - started) * 1000)
            return result

        # The endpoint's main cost control: see ten results, pay to extract five.
        # An omitted value means DEFAULT_EXTRACT_TOP_K, NOT "extract everything" —
        # it used to mean the latter, which made the deployment's cost ceiling depend
        # on every caller remembering the field. Explicit 0 still means "none", so
        # unset and zero stay distinguishable.
        requested = (
            self.settings.default_extract_top_k if extract_top_k is None else extract_top_k
        )
        limit = min(requested, len(items))
        targets, blocked = await self._select_targets(items, limit)
        # Excluded by policy rather than merely unselected — say so, so a caller can
        # tell "we didn't try" from "we tried and it was refused".
        for item in blocked:
            item.status = "skipped"
        if not targets:
            result.took_ms = int((time.perf_counter() - started) * 1000)
            return result

        extractions = await self.extract.extract_many(
            [item.url for item in targets],
            max_tier=max_tier,
            bypass_cache=bypass_cache,
            deadline_s=deadline_s or self._default_deadline(),
        )

        for item, extraction in zip(targets, extractions, strict=True):
            page = extraction.page
            item.status = page.status
            item.extractor_used = page.extractor_used
            item.from_cache = extraction.cache is CacheState.HIT
            if page.status == "ok" and page.markdown:
                item.markdown = page.markdown
                # A page title is usually better than a search-result title.
                if page.title:
                    item.title = page.title
                result.extracted_ok += 1
            if extraction.cache is CacheState.HIT:
                result.pages_from_cache += 1

        result.extracted_attempted = len(targets)
        result.took_ms = int((time.perf_counter() - started) * 1000)

        log.info(
            "pipeline.completed",
            query=query,
            results=len(items),
            attempted=result.extracted_attempted,
            extracted=result.extracted_ok,
            pages_cached=result.pages_from_cache,
            provider=str(outcome.provider),
            took_ms=result.took_ms,
        )
        return result

    async def _select_targets(
        self, items: list[ResultItem], limit: int
    ) -> tuple[list[ResultItem], list[ResultItem]]:
        """Pick up to `limit` extractable results in rank order.

        Returns (targets, excluded_by_policy).

        `items[:limit]` spends the budget before knowing which URLs can be extracted
        at all, so a robots-disallowed result consumes a slot and returns nothing.
        Google ranks Reddit highly and Reddit disallows crawling: a live sample had 3
        of 9 top-3 URLs on reddit.com, i.e. up to 3 of 5 slots wasted.

        Probes the whole candidate list in one concurrent step rather than serially,
        so the cost is a single robots round trip at worst and usually zero.
        """
        if limit <= 0:
            return [], []

        decisions = await asyncio.gather(
            *(self.extract.allows(item.url) for item in items),
            return_exceptions=True,
        )

        targets: list[ResultItem] = []
        blocked: list[ResultItem] = []
        for item, allowed in zip(items, decisions, strict=True):
            if isinstance(allowed, BaseException):
                # A failed probe tells us nothing, and the router re-checks before
                # fetching anyway, so trying beats refusing.
                log.debug("pipeline.robots_probe_failed", url=item.url, error=repr(allowed))
                allowed = True
            if allowed:
                if len(targets) < limit:
                    targets.append(item)
            else:
                blocked.append(item)
            # Stop classifying once the budget is full: results further down were
            # never candidates, and calling them policy-skipped would overstate what
            # was actually refused.
            if len(targets) == limit:
                break

        if blocked:
            log.info(
                "pipeline.skipped_disallowed",
                skipped=len(blocked),
                selected=len(targets),
                limit=limit,
            )
        return targets, blocked

    def _default_deadline(self) -> float:
        """Budget for the whole fan-out.

        An explicit setting rather than a sum of per-page timeouts: those bound how
        long ONE page may take, which is a different question. Deriving it gave 40s
        and a measured p95 of 43s, at which point snippets now beat markdown later.
        """
        return self.settings.extract_deadline_s
