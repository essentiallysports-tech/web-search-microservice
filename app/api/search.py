"""`POST /search` — retrieval only, the cheapest endpoint.

No extraction and no LLM, ever. That separation is the whole cost argument for this
service, so it is enforced by what this module imports rather than by a runtime flag.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import (
    PipelineDep,
    SearchServiceDep,
    SettingsDep,
    resolve_count,
    resolve_max_tier,
)
from app.logging_setup import get_logger, request_id_ctx
from app.models import (
    CacheState,
    ResultItem,
    SearchAndExtractRequest,
    SearchAndExtractResponse,
    SearchRequest,
    SearchResponse,
)
from app.security import authorized_caller
from app.services.search_service import AllProvidersFailedError

log = get_logger(__name__)

router = APIRouter(tags=["search"])


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Ranked URLs and snippets for a query",
)
async def search(
    payload: SearchRequest,
    service: SearchServiceDep,
    settings: SettingsDep,
    caller: Annotated[str, Depends(authorized_caller)],
) -> SearchResponse:
    started = time.perf_counter()
    count = resolve_count(payload.count, settings)

    try:
        outcome = await service.search(
            payload.query,
            count=count,
            lang=payload.lang,
            freshness=payload.freshness,
            bypass_cache=payload.bypass_cache,
        )
    except AllProvidersFailedError as exc:
        log.error("search.all_providers_failed", query=payload.query, attempts=exc.attempts)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "all search providers failed", "attempts": exc.attempts},
        ) from exc

    took_ms = int((time.perf_counter() - started) * 1000)
    from_cache = outcome.cache is CacheState.HIT
    log.info(
        "search.served",
        caller=caller,
        provider=str(outcome.provider),
        results=len(outcome.results),
        degraded=outcome.degraded,
        cache=outcome.cache.value,
        took_ms=took_ms,
    )

    return SearchResponse(
        query=payload.query,
        results=[
            ResultItem(title=r.title, url=r.url, snippet=r.snippet, from_cache=from_cache)
            for r in outcome.results
        ],
        provider=outcome.provider,
        cache=outcome.cache,
        took_ms=took_ms,
        request_id=request_id_ctx.get(),
    )


@router.post(
    "/search_and_extract",
    response_model=SearchAndExtractResponse,
    summary="Search, then extract the top results in parallel",
)
async def search_and_extract(
    payload: SearchAndExtractRequest,
    pipeline: PipelineDep,
    settings: SettingsDep,
    caller: Annotated[str, Depends(authorized_caller)],
) -> SearchAndExtractResponse:
    count = resolve_count(payload.count, settings)

    try:
        outcome = await pipeline.run(
            payload.query,
            count=count,
            lang=payload.lang,
            freshness=payload.freshness,
            extract=payload.extract,
            extract_top_k=payload.extract_top_k,
            max_tier=resolve_max_tier(payload.max_tier, settings),
            bypass_cache=payload.bypass_cache,
            deadline_s=payload.extract_deadline_s,
        )
    except AllProvidersFailedError as exc:
        log.error(
            "search_and_extract.all_providers_failed",
            query=payload.query,
            attempts=exc.attempts,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "all search providers failed", "attempts": exc.attempts},
        ) from exc

    log.info(
        "search_and_extract.served",
        caller=caller,
        provider=str(outcome.provider),
        results=len(outcome.items),
        extracted=outcome.extracted_ok,
        took_ms=outcome.took_ms,
    )

    return SearchAndExtractResponse(
        query=outcome.query,
        results=outcome.items,
        provider=outcome.provider,
        cache=outcome.search_cache,
        extracted=outcome.extracted_ok,
        attempted=outcome.extracted_attempted,
        took_ms=outcome.took_ms,
        request_id=request_id_ctx.get(),
    )
