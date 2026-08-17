"""Search orchestration: provider chain, fallback policy, partial-result rescue.

Cheapest-first (Serper, then Brave). A provider is skipped when its circuit is open
and handed off when it errors or under-returns.

A thin-but-real result set is kept as a rescue value: if Serper returns 2 results
and Brave then fails outright, the caller gets the 2 rather than a 502. Erroring
after a thin primary would throw away a result set already paid for.

Both providers bill per call, so every handoff spends twice for one answer. That is
why the fallback threshold is deliberately low — it should fire on a near-empty
response, not a merely mediocre one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.cache.keys import search_key
from app.cache.layer import CacheLayer
from app.common.circuit import CircuitOpenError
from app.common.metrics import search_fallbacks
from app.config import Settings
from app.logging_setup import get_logger
from app.models import CacheState, Freshness, SearchProviderName, SearchResult
from app.search.base import SearchProvider, SearchProviderError

log = get_logger(__name__)

#: Bumped when the cached envelope shape changes, independently of CACHE_VERSION.
_ENVELOPE_VERSION = 1


class AllProvidersFailedError(RuntimeError):
    """Every configured provider failed. Distinct from "no results found"."""

    def __init__(self, attempts: list[str]) -> None:
        super().__init__("all search providers failed: " + "; ".join(attempts))
        self.attempts = attempts


@dataclass(slots=True)
class HealthReport:
    providers: dict[str, str]
    cache: str | None = None

    def as_dict(self) -> dict[str, str]:
        merged = dict(self.providers)
        if self.cache is not None:
            merged["cache"] = self.cache
        return merged


@dataclass(slots=True)
class SearchOutcome:
    results: list[SearchResult]
    provider: SearchProviderName | None
    #: True when results came from a provider that under-returned, i.e. the
    #: caller is looking at a rescued partial rather than a clean result set.
    degraded: bool = False
    attempted: list[str] = field(default_factory=list)
    #: How this response was served. Set by the caching wrapper.
    cache: CacheState = CacheState.BYPASS

    def to_envelope(self) -> dict[str, Any]:
        """Cache representation. `attempted` is deliberately not stored — it
        describes one request's provider chain, not the cached result."""
        return {
            "v": _ENVELOPE_VERSION,
            "provider": str(self.provider) if self.provider else None,
            "degraded": self.degraded,
            "results": [r.model_dump(mode="json") for r in self.results],
        }

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> SearchOutcome:
        provider = envelope.get("provider")
        return cls(
            results=[SearchResult.model_validate(r) for r in envelope.get("results", [])],
            provider=SearchProviderName(provider) if provider else None,
            degraded=bool(envelope.get("degraded")),
        )


class SearchService:
    def __init__(
        self,
        settings: Settings,
        providers: list[SearchProvider],
        cache: CacheLayer | None = None,
    ) -> None:
        self.settings = settings
        # Disabled providers (missing API key, switched off) are dropped once at
        # construction rather than re-checked on every request.
        self.providers = [p for p in providers if p.enabled]
        if not self.providers:
            raise RuntimeError("SearchService constructed with no enabled providers")
        self.cache = cache
        log.info(
            "search_service.ready",
            providers=[str(p.name) for p in self.providers],
            cache=bool(cache and cache.enabled),
        )

    async def search(
        self,
        query: str,
        *,
        count: int,
        lang: str = "en",
        freshness: Freshness = Freshness.ANY,
        bypass_cache: bool = False,
    ) -> SearchOutcome:
        """Cached search. A cache hit costs nothing and touches no provider."""
        if self.cache is None or not self.cache.enabled:
            outcome = await self.search_uncached(
                query, count=count, lang=lang, freshness=freshness
            )
            outcome.cache = CacheState.BYPASS
            return outcome

        key = search_key(
            query,
            count=count,
            lang=lang,
            freshness=str(freshness),
            version=self.settings.cache_version,
            aggressive=self.settings.cache_aggressive_query_key,
        )

        async def compute() -> dict[str, Any]:
            outcome = await self.search_uncached(
                query, count=count, lang=lang, freshness=freshness
            )
            return outcome.to_envelope()

        envelope, state = await self.cache.get_or_compute(
            key,
            compute,
            ttl=lambda env: self._ttl_for(env, freshness),
            namespace="search",
            bypass=bypass_cache,
        )

        outcome = SearchOutcome.from_envelope(envelope)
        outcome.cache = state
        return outcome

    def _ttl_for(self, envelope: dict[str, Any], freshness: Freshness) -> int:
        """TTL by content class.

        Degraded results are shortest-lived: serving a rescued partial for an hour
        turns one bad minute upstream into an hour of bad answers. Freshness-
        constrained queries are next, since the caller said recency matters.
        """
        if envelope.get("degraded"):
            return self.settings.cache_ttl_search_degraded
        if freshness is not Freshness.ANY:
            return self.settings.cache_ttl_search_fresh
        return self.settings.cache_ttl_search

    async def search_uncached(
        self,
        query: str,
        *,
        count: int,
        lang: str = "en",
        freshness: Freshness = Freshness.ANY,
    ) -> SearchOutcome:
        """Provider chain with fallback. No caching, no coalescing."""
        attempted: list[str] = []
        rescue: SearchOutcome | None = None
        last = len(self.providers) - 1

        # A provider that returned everything asked for has NOT under-returned, so
        # the threshold can never exceed `count`. Without the clamp any request with
        # count < MIN_ACCEPTABLE_RESULTS was unsatisfiable by construction: providers
        # slice to `count`, so count=1 returned 1, lost against a threshold of 3, and
        # walked the whole chain — then cached under the 120s degraded TTL, re-paying
        # every provider every two minutes. Traps #8.
        threshold = min(count, self.settings.min_acceptable_results)

        for index, provider in enumerate(self.providers):
            label = str(provider.name)
            next_label = str(self.providers[index + 1].name) if index < last else "none"

            try:
                results = await provider.search(
                    query, count=count, lang=lang, freshness=freshness
                )
            except CircuitOpenError:
                attempted.append(f"{label}: circuit open")
                search_fallbacks.labels(label, next_label, "circuit_open").inc()
                continue
            except SearchProviderError as exc:
                attempted.append(f"{label}: {exc}")
                log.warning("search.provider_failed", provider=label, error=str(exc))
                search_fallbacks.labels(label, next_label, "error").inc()
                continue

            if len(results) >= threshold:
                return SearchOutcome(
                    results=results[:count],
                    provider=provider.name,
                    degraded=False,
                    attempted=attempted,
                )

            # Under-returned. Keep only if it beats the best partial so far, so the
            # last provider's thin answer can't displace a better earlier one.
            attempted.append(f"{label}: only {len(results)} results")
            if rescue is None or len(results) > len(rescue.results):
                rescue = SearchOutcome(
                    results=results[:count],
                    provider=provider.name,
                    degraded=True,
                    attempted=[],
                )
            if index == last:
                break
            log.info(
                "search.too_few_results",
                provider=label,
                got=len(results),
                threshold=threshold,
            )
            search_fallbacks.labels(label, next_label, "too_few").inc()

        if rescue is not None:
            rescue.attempted = attempted
            log.info("search.served_partial", provider=str(rescue.provider))
            return rescue

        raise AllProvidersFailedError(attempted)

    async def health(self) -> HealthReport:
        """Component health.

        Providers and cache are reported separately because readiness depends only
        on whether a search can be served. A dead cache makes the service more
        expensive, not unavailable, so it must never mask every provider being down.
        """
        providers: dict[str, str] = {}
        for provider in self.providers:
            try:
                alive = await provider.health()
            except Exception:  # a health probe must never take the app down
                alive = False
            breaker = provider.breaker.state
            providers[str(provider.name)] = (
                "ok" if alive and breaker == "closed" else ("degraded" if alive else "down")
            )

        cache_status: str | None = None
        if self.cache is not None and self.cache.enabled:
            cache_status = "ok" if await self.cache.health() else "degraded"

        return HealthReport(providers=providers, cache=cache_status)
