"""Per-host politeness: a concurrency cap and an optional minimum gap between
request starts, per origin.

Global concurrency caps bound load on US; they do nothing to stop ten simultaneous
fetches landing on one origin. Hammering a host is the fastest way to earn the 403
that pushes a page onto the paid tier.

Deliberately in-process: cross-worker coordination would mean a Redis round trip
before every fetch. With N replicas the effective per-host limit is N x the configured
value — documented rather than solved.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from app.logging_setup import get_logger

log = get_logger(__name__)


class _HostState:
    __slots__ = ("semaphore", "next_allowed_at", "lock")

    def __init__(self, concurrency: int) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.next_allowed_at = 0.0
        self.lock = asyncio.Lock()


class HostLimiter:
    def __init__(
        self,
        *,
        concurrency: int = 2,
        min_delay_s: float = 0.0,
        max_tracked_hosts: int = 2048,
    ) -> None:
        self._concurrency = max(1, concurrency)
        self._min_delay_s = max(0.0, min_delay_s)
        self._max_tracked = max_tracked_hosts
        # LRU-bounded so a crawl across many domains can't grow this forever.
        self._hosts: OrderedDict[str, _HostState] = OrderedDict()

    @property
    def tracked_hosts(self) -> int:
        return len(self._hosts)

    def _state_for(self, host: str) -> _HostState:
        state = self._hosts.get(host)
        if state is None:
            state = _HostState(self._concurrency)
            self._hosts[host] = state
            while len(self._hosts) > self._max_tracked:
                # Evicting a host with in-flight requests is safe: the semaphore stays
                # alive in the coroutines holding it, and later arrivals get a new one.
                self._hosts.popitem(last=False)
        else:
            self._hosts.move_to_end(host)
        return state

    @asynccontextmanager
    async def slot(self, url: str):
        host = _host_of(url)
        if host is None:
            yield
            return

        state = self._state_for(host)
        async with state.semaphore:
            if self._min_delay_s > 0:
                await self._wait_turn(state)
            yield

    async def _wait_turn(self, state: _HostState) -> None:
        # The lock serializes the read-modify-write of `next_allowed_at`, so concurrent
        # callers queue instead of all claiming the same slot.
        async with state.lock:
            now = time.monotonic()
            wait_for = state.next_allowed_at - now
            state.next_allowed_at = max(now, state.next_allowed_at) + self._min_delay_s
        if wait_for > 0:
            await asyncio.sleep(wait_for)


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None
