"""`POST /research` — search → extract → LLM synthesis.

The most expensive endpoint and the only one that spends LLM tokens. Unavailable
unless ENABLE_LLM_LAYER=true and a provider is configured, so a default deployment
cannot be billed by it even by mistake.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import (
    PipelineDep,
    SettingsDep,
    get_llm_provider,
    resolve_count,
    resolve_exclude,
    resolve_max_tier,
)
from app.logging_setup import get_logger, request_id_ctx
from app.models import ResearchRequest, ResearchResponse
from app.rerank.base import LLMProvider
from app.rerank.llm import LLMUnavailableError
from app.security import authorized_caller
from app.services.search_service import AllProvidersFailedError

log = get_logger(__name__)

router = APIRouter(tags=["research"])


@router.post(
    "/research",
    response_model=ResearchResponse,
    summary="Search, extract, and synthesize a cited answer",
)
async def research(
    payload: ResearchRequest,
    pipeline: PipelineDep,
    settings: SettingsDep,
    caller: Annotated[str, Depends(authorized_caller)],
    llm: Annotated[LLMProvider | None, Depends(get_llm_provider)],
) -> ResearchResponse:
    if llm is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "LLM layer is disabled",
                "hint": "set ENABLE_LLM_LAYER=true and configure a provider",
            },
        )

    started = time.perf_counter()
    count = resolve_count(payload.count, settings)

    try:
        outcome = await pipeline.run(
            payload.query,
            count=count,
            lang=payload.lang,
            freshness=payload.freshness,
            extract=True,  # synthesis over snippets alone is not worth paying for
            extract_top_k=payload.extract_top_k,
            max_tier=resolve_max_tier(payload.max_tier, settings),
            bypass_cache=payload.bypass_cache,
            exclude=resolve_exclude(payload.exclude_domains),
        )
    except AllProvidersFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "all search providers failed", "attempts": exc.attempts},
        ) from exc

    # Only sources with real extracted content are worth tokens — feeding snippets to
    # the model invites confident answers built on two sentences of preview.
    sources = [item for item in outcome.items if item.markdown]
    if not sources:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "no extractable sources for this query",
                "hint": "results were found but none could be extracted; try bypass_cache or a higher max_tier",
            },
        )

    try:
        result = await llm.synthesize(
            payload.query, sources, instruction=payload.instruction
        )
    except LLMUnavailableError as exc:
        log.error("research.llm_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "synthesis failed", "reason": str(exc)},
        ) from exc

    took_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "research.served",
        caller=caller,
        sources=len(sources),
        citations=len(result.citations),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        took_ms=took_ms,
    )

    return ResearchResponse(
        query=outcome.query,
        results=outcome.items,
        provider=outcome.provider,
        cache=outcome.search_cache,
        answer=result.answer,
        citations=result.citations,
        model=result.model,
        took_ms=took_ms,
        request_id=request_id_ctx.get(),
    )
