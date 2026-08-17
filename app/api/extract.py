"""`POST /extract` — clean text from URLs the caller already has.

The caller controls its own cost: `max_tier` caps how expensive extraction may get
(`http_retry` never bills), and per-page failures never fail the whole request.

Bounded by EXTRACT_BATCH_DEADLINE_S rather than the pipeline's deadline, because this
endpoint takes up to 20 URLs — four waves at EXTRACT_CONCURRENCY=5.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ExtractServiceDep, SettingsDep, resolve_max_tier
from app.logging_setup import get_logger, request_id_ctx
from app.models import (
    CacheState,
    ExtractRequest,
    ExtractResponse,
    ResultItem,
)
from app.security import authorized_caller

log = get_logger(__name__)

router = APIRouter(tags=["extract"])


@router.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Clean, LLM-ready text for one or more URLs",
)
async def extract(
    payload: ExtractRequest,
    service: ExtractServiceDep,
    settings: SettingsDep,
    caller: Annotated[str, Depends(authorized_caller)],
) -> ExtractResponse:
    started = time.perf_counter()
    urls = [str(u) for u in payload.urls]
    deadline_s = settings.extract_batch_deadline_s

    results = await service.extract_many(
        urls,
        max_tier=resolve_max_tier(payload.max_tier, settings),
        # No single page may be given more time than the whole batch has. Without
        # the clamp a caller could send timeout_s=60 and have every page's budget
        # exceed the deadline that is supposed to bound them.
        timeout_s=min(payload.timeout_s, deadline_s) if payload.timeout_s else None,
        bypass_cache=payload.bypass_cache,
        # Bounds total endpoint time, and is also what activates the paid-tier
        # budget guard — the router only checks remaining time when a deadline
        # exists, so before this the guard was inert on /extract.
        deadline_s=deadline_s,
    )

    took_ms = int((time.perf_counter() - started) * 1000)
    ok = sum(1 for r in results if r.page.status == "ok")
    log.info(
        "extract.served",
        caller=caller,
        urls=len(urls),
        ok=ok,
        cached=sum(1 for r in results if r.cache is CacheState.HIT),
        tiers=sum(r.tiers_used for r in results),
        took_ms=took_ms,
    )

    return ExtractResponse(
        results=[
            ResultItem(
                title=r.page.title or "",
                url=r.page.url,
                snippet="",
                markdown=r.page.markdown,
                extractor_used=r.page.extractor_used,
                status=r.page.status,
                from_cache=r.cache is CacheState.HIT,
            )
            for r in results
        ],
        took_ms=took_ms,
        request_id=request_id_ctx.get(),
    )
