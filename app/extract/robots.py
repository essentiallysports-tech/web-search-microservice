"""robots.txt policy.

Running this service makes you a crawler operator, so RESPECT_ROBOTS_TXT defaults on.

Decisions are cached per-origin: fetching robots.txt on every extraction would double
the request count against every host we touch, which is the behaviour robots.txt exists
to discourage.

Failure is permissive by design — the convention is that only an explicit `Disallow`
denies, and an unreachable file means "no rules stated".

KNOWN GAP: `_parsed` is a per-process memo with no bound and no expiry, so it defeats
the 24h Redis TTL for the lifetime of the process. `HostLimiter` bounds its equivalent
map; this doesn't.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.cache.layer import CacheLayer
from app.logging_setup import get_logger

log = get_logger(__name__)

#: robots.txt changes rarely; a day is plenty and keeps request counts low.
ROBOTS_TTL_S = 86400
#: A robots.txt larger than this is pathological. Google caps at 500KB.
MAX_ROBOTS_BYTES = 512 * 1024


class RobotsPolicy:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: CacheLayer | None,
        *,
        user_agent: str,
        enabled: bool = True,
        timeout_s: float = 5.0,
    ) -> None:
        self._client = client
        self._cache = cache
        self._user_agent = user_agent
        self._enabled = enabled
        self._timeout_s = timeout_s
        # Per-process memo so a batch of URLs from one host parses robots once. See the
        # module docstring: unbounded, and it outlives the Redis TTL.
        self._parsed: dict[str, RobotFileParser | None] = {}

    async def allows(self, url: str) -> bool:
        if not self._enabled:
            return True

        origin = _origin(url)
        if origin is None:
            return True

        parser = await self._parser_for(origin)
        if parser is None:
            return True  # unreachable or unparseable: no rules stated

        try:
            return parser.can_fetch(self._user_agent, url)
        except Exception:  # a malformed rule must not break extraction
            return True

    async def _parser_for(self, origin: str) -> RobotFileParser | None:
        if origin in self._parsed:
            return self._parsed[origin]

        text = await self._robots_text(origin)
        parser: RobotFileParser | None = None
        if text is not None:
            parser = RobotFileParser()
            try:
                parser.parse(text.splitlines())
            except Exception as exc:
                log.warning("robots.parse_failed", origin=origin, error=repr(exc))
                parser = None

        self._parsed[origin] = parser
        return parser

    async def _robots_text(self, origin: str) -> str | None:
        key = f"robots:{origin}"

        async def fetch() -> str | None:
            try:
                response = await self._client.get(
                    f"{origin}/robots.txt", timeout=self._timeout_s
                )
            except httpx.HTTPError as exc:
                log.debug("robots.fetch_failed", origin=origin, error=repr(exc))
                return None

            # 4xx (404 included) conventionally means "no restrictions". 5xx
            # conventionally means "assume disallowed", but treating a flaky server as a
            # permanent block is worse in practice, so stay permissive and cache nothing.
            if response.status_code >= 500:
                return None
            if response.status_code >= 400:
                return ""
            return response.text[:MAX_ROBOTS_BYTES]

        if self._cache is None or not self._cache.enabled:
            return await fetch()

        value, _ = await self._cache.get_or_compute(
            key, fetch, ttl=ROBOTS_TTL_S, namespace="robots"
        )
        return value


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"
