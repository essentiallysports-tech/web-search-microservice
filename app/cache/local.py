"""In-process L1 cache.

A Redis round trip is ~0.2-1ms; a dict lookup is ~100ns. For the hot queries that
dominate real traffic this removes the round trip and the load on a Redis instance
shared by every replica.

Deliberately small and short-lived — a latency optimization, not the cache. Redis
stays the source of truth, so a stale entry is at most LOCAL_CACHE_TTL_S out of date.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

_MISS = object()


class TTLCache:
    """LRU with a per-entry TTL. Not thread-safe; asyncio single-thread only."""

    def __init__(self, maxsize: int, ttl_s: float) -> None:
        self._maxsize = max(0, maxsize)
        self._ttl_s = ttl_s
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._maxsize > 0 and self._ttl_s > 0

    def get(self, key: str) -> Any:
        """Return the value, or the module-level `_MISS` sentinel."""
        entry = self._entries.get(key)
        if entry is None:
            return _MISS

        expires_at, value = entry
        if expires_at <= time.monotonic():
            # Lazy eviction on read; there is no sweeper task.
            del self._entries[key]
            return _MISS

        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._entries[key] = (time.monotonic() + self._ttl_s, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


MISS = _MISS
