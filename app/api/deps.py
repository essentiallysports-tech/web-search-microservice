"""Shared route dependencies.

Everything expensive is built once in the lifespan and read off `app.state` here.
Nothing in a request path constructs a client or a pool.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.models import ExtractorName
from app.rerank.base import LLMProvider
from app.search.domains import parse_domains
from app.services.extract_service import ExtractService
from app.services.pipeline import SearchExtractPipeline
from app.services.search_service import SearchService


def get_search_service(request: Request) -> SearchService:
    return request.app.state.search_service


def get_extract_service(request: Request) -> ExtractService:
    return request.app.state.extract_service


def get_pipeline(request: Request) -> SearchExtractPipeline:
    return request.app.state.pipeline


def get_llm_provider(request: Request) -> LLMProvider | None:
    """None when the LLM layer is off — the route turns that into a 503."""
    return request.app.state.llm_provider


def get_app_settings(request: Request) -> Settings:
    """The settings this app booted with.

    Deliberately not `config.get_settings()` — that is an lru_cache'd process global,
    so a route bound to it ignores how the running app was actually configured.
    """
    return request.app.state.settings


def resolve_count(requested: int | None, settings: Settings) -> int:
    """None means the caller didn't ask, so DEFAULT_RESULT_COUNT applies.
    MAX_RESULT_COUNT is the hard ceiling either way."""
    return min(requested or settings.default_result_count, settings.max_result_count)


def resolve_exclude(requested: list[str] | None) -> frozenset[str] | None:
    """Normalize the caller's `exclude_domains` for the service layer.

    None stays None so SEARCH_BLOCKED_DOMAINS applies; an explicit empty list
    becomes an empty set, which the service honours as "filtering off".
    """
    if requested is None:
        return None
    return parse_domains(",".join(requested))


def resolve_max_tier(requested: ExtractorName | None, settings: Settings) -> ExtractorName:
    """Apply the deployment's tier ceiling when the caller omits one.

    Not a hard cap — a caller asking for `firecrawl` gets it. The default exists so
    reaching the PAID tier is a decision someone made rather than what happens when
    nobody says anything.
    """
    return requested or settings.default_max_tier


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
ExtractServiceDep = Annotated[ExtractService, Depends(get_extract_service)]
PipelineDep = Annotated[SearchExtractPipeline, Depends(get_pipeline)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
