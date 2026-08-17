"""FastAPI application factory and route wiring.

Expensive, reusable objects (HTTP clients, Redis pool, provider instances) are
created once in the lifespan and hung off `app.state`. Nothing in a request path
is allowed to construct a connection pool.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__
from app.api import admin as admin_routes
from app.api import extract as extract_routes
from app.api import research as research_routes
from app.api import search as search_routes
from app.cache.codec import Codec
from app.cache.layer import CacheLayer
from app.cache.redis_cache import RedisCache
from app.common.metrics import circuit_state, inflight_requests, request_duration
from app.common.ratelimit import RateLimiter
from app.config import Settings, get_settings
from app.extract.base import ExtractProvider
from app.extract.firecrawl_ext import FirecrawlExtractor
from app.extract.http_retry import HttpRetryExtractor
from app.extract.robots import RobotsPolicy
from app.extract.router import ExtractRouter
from app.extract.trafilatura_ext import TrafilaturaExtractor
from app.http_client import (
    build_client,
    build_extraction_client,
    close_client,
    set_client,
    set_extraction_client,
)
from app.logging_setup import configure_logging, get_logger, request_id_ctx
from app.models import HealthResponse
from app.rerank.llm import build_llm_provider
from app.search.base import SearchProvider
from app.search.brave import BraveProvider
from app.search.serper import SerperProvider
from app.services.extract_service import ExtractService
from app.services.pipeline import SearchExtractPipeline
from app.services.search_service import SearchService
from app.tokens import TokenStore

log = get_logger(__name__)

_CIRCUIT_STATE_VALUE = {"closed": 0.0, "half_open": 1.0, "open": 2.0}


def build_search_providers(settings: Settings) -> list[SearchProvider]:
    """Providers in fallback order — cheapest first. The ordering IS the cost policy.

    Serper is ~$1/1k on Google's index, so it leads on both price and quality. Brave is
    ~5x dearer and runs only when Serper errors or under-returns; it is worth keeping
    because it is an independent index, so it fails differently than the primary.

    Both drop out on their own when unconfigured.
    """
    providers: list[SearchProvider] = [
        SerperProvider(settings),
        BraveProvider(settings),
    ]
    return [p for p in providers if p.enabled]


def build_extractors(settings: Settings) -> list[ExtractProvider]:
    """The tier ladder, cheapest first.

    The router sorts and filters this, so ordering here is documentation rather
    than policy. An unconfigured tier (no Firecrawl key) drops out on its own via
    `enabled`, which leaves a free-only ladder rather than a broken one.
    """
    return [
        TrafilaturaExtractor(settings),
        HttpRetryExtractor(settings),
        FirecrawlExtractor(settings),
    ]


async def build_cache(settings: Settings) -> CacheLayer:
    """Build the cache layer, tolerating an unreachable Redis.

    An unreachable cache must not stop the service booting — that would turn a cost
    problem into an outage.
    """
    if not settings.cache_enabled:
        log.info("cache.disabled_by_config")
        return CacheLayer(settings, None)

    from redis.asyncio import Redis

    client = Redis.from_url(
        settings.redis_url,
        decode_responses=False,  # payloads are zstd-framed bytes, not text
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=30,
    )
    redis_cache = RedisCache(
        client,
        Codec(
            min_compress_bytes=settings.cache_compress_min_bytes,
            level=settings.cache_compress_level,
        ),
        fail_threshold=settings.circuit_fail_threshold,
        reset_after_s=settings.circuit_reset_after_s,
    )
    if await redis_cache.ping():
        log.info("cache.connected", url=settings.redis_url)
    else:
        log.warning("cache.unreachable_at_startup", url=settings.redis_url)
    return CacheLayer(settings, redis_cache)


def _validate_startup(settings: Settings) -> None:
    """Fail fast on configurations that are wrong in a way we can detect."""
    if settings.environment == "prod":
        if settings.auth_enabled and not settings.service_api_keys:
            raise RuntimeError("AUTH_ENABLED=true in prod but SERVICE_API_KEYS is empty")
        if not settings.auth_enabled:
            log.warning("startup.auth_disabled_in_prod")
        if not settings.auth_enabled and settings.rate_limit_enabled:
            # Every caller shares the "anonymous" identity, so the per-key budget is
            # really one global budget.
            log.warning("startup.rate_limit_without_auth_is_global")
    if not settings.serper_api_key and not settings.brave_api_key:
        raise RuntimeError(
            "no search provider configured: set SERPER_API_KEY (primary) or BRAVE_API_KEY"
        )
    if not settings.serper_api_key:
        # Serviceable, but every search runs on the ~5x dearer provider. A bill, not an
        # outage, so warn rather than refuse to boot.
        log.warning("startup.no_serper_key_running_on_brave_only")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    _validate_startup(settings)

    app.state.settings = settings
    client = build_client(settings)
    set_client(client)
    app.state.http = client

    # Separate client for third-party page fetches, so a residential proxy applies only
    # where it is needed and is not billed for search API traffic.
    extraction_client = build_extraction_client(settings)
    set_extraction_client(extraction_client)
    if settings.proxy_url and settings.proxy_for_extraction:
        log.info("extraction.proxy_enabled")

    cache = await build_cache(settings)
    app.state.cache = cache
    # Shares Redis with the cache: an in-process limiter would give every replica its
    # own full budget, making the effective limit N x configured.
    app.state.rate_limiter = RateLimiter(settings, cache.redis)
    # Dynamic API tokens live in the same Redis. Static SERVICE_API_KEYS keep working
    # without it, which is why they remain the admin and break-glass path.
    app.state.token_store = TokenStore(cache.redis, version=settings.cache_version)
    app.state.search_service = SearchService(
        settings, build_search_providers(settings), cache
    )

    robots = RobotsPolicy(
        client,
        cache,
        user_agent=settings.user_agent,
        enabled=settings.respect_robots_txt,
    )
    extract_router = ExtractRouter(settings, build_extractors(settings), robots)
    # No-op for the shipped tiers — they are all stateless HTTP. Kept because the
    # ExtractProvider contract has the hook and a future tier may need it.
    await extract_router.startup()
    app.state.extract_router = extract_router
    app.state.extract_service = ExtractService(settings, extract_router, cache)
    app.state.pipeline = SearchExtractPipeline(
        settings, app.state.search_service, app.state.extract_service
    )
    # None unless explicitly enabled AND configured, so /research 503s rather than
    # silently costing money.
    app.state.llm_provider = build_llm_provider(settings)

    log.info(
        "startup",
        service=settings.service_name,
        environment=settings.environment,
        version=__version__,
        serper=bool(settings.serper_api_key),
        brave=bool(settings.brave_api_key),
        firecrawl=bool(settings.firecrawl_api_key),
        llm=settings.enable_llm_layer,
        cache=settings.cache_enabled,
    )

    try:
        yield
    finally:
        await extract_router.shutdown()
        await close_client()
        await cache.close()
        log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    # No custom response class: current FastAPI serializes straight to JSON bytes
    # through the response model, which beats routing through orjson ourselves. orjson
    # stays a dependency for cache payload encoding.
    app = FastAPI(
        title="Web Search Service",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "prod" else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def observability(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(request_id)
        endpoint = request.url.path
        started = time.perf_counter()
        inflight_requests.labels(endpoint).inc()
        try:
            response = await call_next(request)
        except Exception:
            request_duration.labels(endpoint, "500").observe(time.perf_counter() - started)
            log.exception("request.unhandled", method=request.method, path=endpoint)
            raise
        finally:
            inflight_requests.labels(endpoint).dec()
            request_id_ctx.reset(token)

        elapsed = time.perf_counter() - started
        request_duration.labels(endpoint, str(response.status_code)).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed * 1000:.1f}"
        return response

    app.include_router(search_routes.router)
    app.include_router(extract_routes.router)
    app.include_router(research_routes.router)
    app.include_router(admin_routes.router)

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    async def health(request: Request, response: Response) -> HealthResponse:
        service: SearchService = request.app.state.search_service
        report = await service.health()
        extractors = await request.app.state.extract_service.health()

        # Piggyback the breaker gauges on the health probe — it is polled on a schedule
        # anyway, so this needs no separate timer.
        for provider in service.providers:
            circuit_state.labels(str(provider.name)).set(
                _CIRCUIT_STATE_VALUE.get(str(provider.breaker.state), 0.0)
            )

        # Providers only. A degraded cache costs money, not availability, so it must
        # never mask every provider being down.
        provider_states = report.providers.values()
        if any(state == "ok" for state in provider_states):
            overall = "ok"
        elif any(state == "degraded" for state in provider_states):
            overall = "degraded"
        else:
            overall = "down"

        # Readiness: a load balancer should stop sending traffic when no provider can
        # serve. /livez stays 200 so the container isn't killed for a recoverable
        # upstream outage.
        if overall == "down":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        # Extractor health is reported but excluded from `overall` for the same reason
        # as the cache: /search does not depend on it.
        return HealthResponse(
            status=overall,
            providers={**report.as_dict(), **extractors},
            version=__version__,
        )

    @app.get("/livez", tags=["ops"], response_class=PlainTextResponse)
    async def livez() -> str:
        return "ok"

    if settings.metrics_enabled:

        @app.get("/metrics", tags=["ops"], include_in_schema=False)
        async def metrics() -> PlainTextResponse:
            return PlainTextResponse(
                generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST
            )

    return app


app = create_app()
