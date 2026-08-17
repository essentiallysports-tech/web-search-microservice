"""In-process request coalescing.

Ten concurrent callers asking for the same uncached query should cause one upstream
call, not ten — on a paid provider each duplicate is money. `CacheLayer` pairs this
with a Redis lock to extend the guarantee across replicas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.logging_setup import get_logger

log = get_logger(__name__)


class InProcessSingleFlight:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    async def do(
        self, key: str, factory: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, bool]:
        """Run `factory`, or join an identical call already running.

        Returns (value, joined); `joined` is True for callers that piggybacked on
        someone else's work.
        """
        existing = self._inflight.get(key)
        if existing is not None:
            # shield() so cancelling THIS waiter doesn't cancel the shared future the
            # other waiters depend on.
            return await asyncio.shield(existing), True

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        # Mark any exception retrieved so a leader failing with no waiters doesn't log
        # "exception was never retrieved". Waiters still see it raised from their await.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self._inflight[key] = future

        try:
            value = await factory()
        except BaseException as exc:
            # BaseException covers CancelledError: a waiter blocked on a cancelled
            # leader must be woken, not left hanging until its own timeout.
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(value)
            return value, False
        finally:
            self._inflight.pop(key, None)
