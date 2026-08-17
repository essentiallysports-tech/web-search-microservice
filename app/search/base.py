"""SearchProvider interface.

Every provider normalizes into `SearchResult`, so swapping Serper for Brave (or
adding a third) never changes the API surface or the cache format.

Providers raise `SearchProviderError` for anything the orchestrator should treat
as "try the next provider". They must not raise provider-specific exception
types past this boundary.
"""

from __future__ import annotations

import abc
import time

import httpx

from app.common.circuit import CircuitBreaker, CircuitOpenError
from app.common.metrics import (
    external_calls,
    provider_duration,
    search_provider_calls,
    search_results_returned,
)
from app.common.retry import with_retries
from app.config import Settings
from app.http_client import get_client
from app.logging_setup import get_logger
from app.models import Freshness, SearchProviderName, SearchResult

log = get_logger(__name__)


class SearchProviderError(RuntimeError):
    """Recoverable provider failure — the orchestrator should fall back."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


class SearchProvider(abc.ABC):
    """One search backend."""

    #: Stable identifier used in metrics, logs, and the response `provider` field.
    name: SearchProviderName

    #: True when a call to this provider costs money. Drives the cost counters.
    billable: bool = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self.breaker = CircuitBreaker(
            str(self.name),
            fail_threshold=settings.circuit_fail_threshold,
            reset_after_s=settings.circuit_reset_after_s,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """The shared process client, unless one was injected (tests)."""
        return self._client if self._client is not None else get_client()

    # ---------------------------------------------------------------- contract

    @abc.abstractmethod
    async def _search(
        self,
        query: str,
        *,
        count: int,
        lang: str,
        freshness: Freshness,
    ) -> list[SearchResult]:
        """Perform the actual call. Raise `SearchProviderError` on failure."""

    @abc.abstractmethod
    async def health(self) -> bool:
        """Cheap liveness probe used by /health. Must not raise."""

    @property
    def enabled(self) -> bool:
        """False when the provider is unconfigured (e.g. missing API key)."""
        return True

    # ------------------------------------------------------------- public path

    async def search(
        self,
        query: str,
        *,
        count: int,
        lang: str = "en",
        freshness: Freshness = Freshness.ANY,
    ) -> list[SearchResult]:
        """Circuit-broken, instrumented wrapper around `_search`."""
        if not self.breaker.allows():
            search_provider_calls.labels(str(self.name), "circuit_open").inc()
            raise CircuitOpenError(str(self.name))

        started = time.perf_counter()
        try:
            # Retries sit inside the breaker: a call that succeeds on its second
            # attempt is a success, and an exhausted retry is one failure — not
            # `retry_attempts` of them.
            results = await with_retries(
                lambda: self._search(query, count=count, lang=lang, freshness=freshness),
                attempts=self.settings.retry_attempts,
                backoff_s=self.settings.retry_backoff_s,
                label=str(self.name),
            )
        except CircuitOpenError:
            raise
        except SearchProviderError:
            await self.breaker.record_failure()
            search_provider_calls.labels(str(self.name), "error").inc()
            raise
        except Exception as exc:  # unexpected — still a provider failure
            await self.breaker.record_failure()
            search_provider_calls.labels(str(self.name), "error").inc()
            raise SearchProviderError(str(self.name), repr(exc)) from exc
        finally:
            provider_duration.labels(str(self.name)).observe(time.perf_counter() - started)
            external_calls.labels(str(self.name), str(self.billable).lower()).inc()

        await self.breaker.record_success()
        search_provider_calls.labels(str(self.name), "ok" if results else "empty").inc()
        search_results_returned.labels(str(self.name)).observe(len(results))
        return results
