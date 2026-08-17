"""Per-provider circuit breaker.

A dead provider should cost one fast rejection, not a full timeout per request:
without this a Serper outage turns every call into an 8-second wait before the Brave
fallback even starts.

closed -> open (reject immediately) -> half_open (one probe). A successful probe
closes it; a failed probe re-opens it.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum

from app.logging_setup import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is open."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit open for provider {name!r}")
        self.provider = name


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int = 5,
        reset_after_s: float = 60.0,
    ) -> None:
        self.name = name
        self._fail_threshold = fail_threshold
        self._reset_after_s = reset_after_s
        self._failures = 0
        self._opened_at = 0.0
        self._state = CircuitState.CLOSED
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        # Lazy OPEN -> HALF_OPEN once the cooldown elapses, so readers see the truth
        # without needing a timer task.
        if self._state is CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self._reset_after_s:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allows(self) -> bool:
        return self.state is not CircuitState.OPEN

    async def record_success(self) -> None:
        # Lock-free fast path: "already healthy" is the overwhelmingly common case and
        # this runs on every cache read, the hottest path in the service.
        if self._state is CircuitState.CLOSED and self._failures == 0:
            return
        async with self._lock:
            if self._state is not CircuitState.CLOSED:
                log.info("circuit.closed", provider=self.name)
            self._failures = 0
            self._state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self._fail_threshold:
                if self._state is not CircuitState.OPEN:
                    log.warning(
                        "circuit.opened", provider=self.name, failures=self._failures
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        return {"state": str(self.state), "failures": self._failures}
