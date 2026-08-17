"""Retries with exponential backoff and jitter.

Only RETRYABLE failures are retried, which is the whole point: a 401 means the key is
wrong, so retrying spends three times the latency to fail identically. A 503 or a
timeout is worth another go.

Jitter matters more than it looks — without it a batch that fails together retries
together, which against a rate-limited upstream turns one 429 into three.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.common.metrics import retries_total
from app.logging_setup import get_logger

log = get_logger(__name__)

T = TypeVar("T")


def is_retryable(exc: BaseException) -> bool:
    """Honour an explicit `retryable` flag when present.

    Providers set `SearchProviderError.retryable`, so config errors are never retried.
    Defaults to False: an unlabelled exception is not assumed safe to repeat.
    """
    return bool(getattr(exc, "retryable", False))


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    backoff_s: float,
    label: str,
    should_retry: Callable[[BaseException], bool] = is_retryable,
) -> T:
    """Run `operation`, retrying transient failures.

    `attempts` counts total tries, not extra ones: 1 means no retry.
    """
    if attempts <= 1:
        return await operation()

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=backoff_s, max=backoff_s * 8),
        retry=retry_if_exception(should_retry),
        reraise=True,  # surface the original error, not tenacity's RetryError
        before_sleep=_log_retry(label),
    ):
        with attempt:
            return await operation()

    raise AssertionError("unreachable: AsyncRetrying always returns or raises")


def _log_retry(label: str):
    def hook(state) -> None:
        retries_total.labels(label).inc()
        log.warning(
            "retry",
            operation=label,
            attempt=state.attempt_number,
            error=repr(state.outcome.exception()) if state.outcome else None,
        )

    return hook
