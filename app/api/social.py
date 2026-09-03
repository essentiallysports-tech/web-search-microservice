"""`POST /social_search` — Twitter or Reddit, explicitly chosen.

Deliberately not folded into `/search`'s provider chain: Twitter and Reddit are
not fallbacks for each other or for web search, they're distinct capabilities a
caller picks by name. Reuses `SearchService` per platform (each holding exactly
one provider) rather than a parallel implementation — same caching, circuit
breaking, and budget enforcement `/search` already has, for free.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.logging_setup import get_logger, request_id_ctx
from app.models import CacheState, ResultItem, SearchResponse, SocialSearchRequest
from app.security import authorized_caller
from app.services.search_service import AllProvidersFailedError, SearchService

log = get_logger(__name__)

router = APIRouter(tags=["social"])


def _service_for(request: Request, platform: str) -> SearchService | None:
    return getattr(request.app.state, f"{platform}_service", None)


@router.post(
    "/social_search",
    response_model=SearchResponse,
    summary="Twitter or Reddit search, explicitly chosen by platform",
)
async def social_search(
    payload: SocialSearchRequest,
    request: Request,
    caller: Annotated[str, Depends(authorized_caller)],
) -> SearchResponse:
    service = _service_for(request, payload.platform)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": f"{payload.platform} search is not configured",
                "hint": f"set {payload.platform.upper()}API_API_KEY"
                if payload.platform == "twitter"
                else "set REDDITAPIS_API_KEY",
            },
        )

    started = time.perf_counter()
    count = payload.count or 10

    try:
        outcome = await service.search(
            payload.query,
            count=count,
            freshness=payload.freshness,
            bypass_cache=payload.bypass_cache,
        )
    except AllProvidersFailedError as exc:
        log.error(
            "social_search.failed", platform=payload.platform, query=payload.query, attempts=exc.attempts
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": f"{payload.platform} search failed", "attempts": exc.attempts},
        ) from exc

    took_ms = int((time.perf_counter() - started) * 1000)
    from_cache = outcome.cache is CacheState.HIT
    log.info(
        "social_search.served",
        caller=caller,
        platform=payload.platform,
        results=len(outcome.results),
        cache=outcome.cache.value,
        took_ms=took_ms,
    )

    return SearchResponse(
        query=payload.query,
        results=[
            ResultItem(
                title=r.title, url=r.url, snippet=r.snippet,
                from_cache=from_cache, published_at=r.published_at,
            )
            for r in outcome.results
        ],
        provider=outcome.provider,
        cache=outcome.cache,
        took_ms=took_ms,
        request_id=request_id_ctx.get(),
    )
