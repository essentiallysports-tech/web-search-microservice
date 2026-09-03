"""Real-dollar spend ceiling — daily and monthly.

WHY THIS EXISTS

Every other guard in this service bounds request RATE (`ratelimit.py`) or
per-page TIME (extraction deadlines). Nothing bounds money. `HANDOFF.md`'s own
Open Issue #2 flags this — "nothing caps cumulative spend... a client looping
with `bypass_cache` takes the bill from $0.41 to $1.97 per 1k" — and it was
never closed before this file existed.

This is deliberately checked at TWO points per billable call, same shape as
`checkByteBudget`/`chargeBytes` in the sibling `es-mcp` project's Athena guard:

  check(provider)   BEFORE the call — cost 0, only asks "is the bucket already
                     over?". Cheap, no external call made if it refuses.
  charge(provider, usd)  AFTER the call, from whatever real cost the provider
                     itself reported (Serper's credits, an actor's own price,
                     or this service's own list-price estimate when a
                     provider doesn't report one). Best-effort: a failed
                     charge must never fail a request that already spent
                     real money.

FAILS CLOSED, NOT OPEN — a deliberate exception to this codebase's own
"everything fails open" rule (`cache/redis_cache.py`'s module docstring).
That rule exists because losing the cache only makes the service slower and
more expensive, never wrong. A spend ceiling is the opposite: failing open
on it means an outage makes it MORE likely to overspend at exactly the
moment nobody can see the meter, which is how the Apify bill in production
practice ended up far past its own headline per-item price. If Redis is
unreachable, `check()` refuses rather than guesses.

Two windows because a request that clears "today" can still blow the month,
and vice versa on the 1st.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.logging_setup import get_logger

log = get_logger(__name__)

# Atomic increment-and-read. A Lua script rather than INCRBYFLOAT + GET as two
# calls, because two calls racing under load could both read a pre-increment
# value and both decide they are still under budget — same class of race the
# existing `try_lock` script avoids for the cache lock.
_CHARGE_LUA = """
local new_total = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
if tonumber(redis.call('TTL', KEYS[1])) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return new_total
"""


class BudgetExceededError(RuntimeError):
    """A billable call was refused because a spend window is already full."""

    def __init__(self, window: str, spent_usd: float, cap_usd: float) -> None:
        super().__init__(
            f"{window} budget exhausted: ${spent_usd:.2f} of ${cap_usd:.2f} spent"
        )
        self.window = window
        self.spent_usd = spent_usd
        self.cap_usd = cap_usd


class BudgetUnavailableError(RuntimeError):
    """Redis could not be reached — refused rather than guessed. See module docstring."""


@dataclass(slots=True)
class _Window:
    label: str
    key_fn: "callable[[float], str]"
    ttl_s: int
    cap_usd: float


def _day_key(now: float) -> str:
    return time.strftime("wss:budget:day:%Y-%m-%d", time.gmtime(now))


def _month_key(now: float) -> str:
    return time.strftime("wss:budget:month:%Y-%m", time.gmtime(now))


class Budget:
    """Daily + monthly spend ceiling, shared across every billable provider."""

    def __init__(
        self,
        client: Redis | None,
        *,
        daily_cap_usd: float,
        monthly_cap_usd: float,
    ) -> None:
        self._client = client
        self._charge_script = client.register_script(_CHARGE_LUA) if client else None
        self._windows: list[_Window] = []
        if daily_cap_usd > 0:
            self._windows.append(_Window("daily", _day_key, 2 * 86400, daily_cap_usd))
        if monthly_cap_usd > 0:
            # 32 days of TTL headroom rather than a precise month length — the key
            # itself is month-stamped, so a slightly generous TTL just means a
            # finished month's key lingers a little before Redis reclaims it.
            self._windows.append(_Window("monthly", _month_key, 32 * 86400, monthly_cap_usd))

    @property
    def enabled(self) -> bool:
        return bool(self._windows)

    async def check(self) -> None:
        """Refuse if any configured window is already at or past its cap.

        Cost-0: reads only, never increments. Raises `BudgetExceededError` if
        over, `BudgetUnavailableError` if Redis can't be reached — both refuse
        the caller from proceeding, deliberately, see module docstring.
        """
        if not self.enabled:
            return
        if self._client is None:
            raise BudgetUnavailableError("no Redis configured for the budget")

        now = time.time()
        for window in self._windows:
            key = window.key_fn(now)
            try:
                raw = await self._client.get(key)
            except RedisError as exc:
                log.warning("budget.check_unavailable", window=window.label, error=repr(exc))
                raise BudgetUnavailableError(f"budget check failed: {exc!r}") from exc
            spent = float(raw) if raw is not None else 0.0
            if spent >= window.cap_usd:
                log.error(
                    "budget.exceeded", window=window.label, spent_usd=spent, cap_usd=window.cap_usd
                )
                raise BudgetExceededError(window.label, spent, window.cap_usd)

    async def charge(self, provider: str, usd: float) -> None:
        """Record real spend. Best-effort — see module docstring."""
        if not self.enabled or usd <= 0 or self._client is None or self._charge_script is None:
            return
        now = time.time()
        for window in self._windows:
            key = window.key_fn(now)
            try:
                total = await self._charge_script(keys=[key], args=[usd, window.ttl_s])
            except RedisError as exc:
                log.warning(
                    "budget.charge_failed", provider=provider, window=window.label, error=repr(exc)
                )
                continue
            total_f = float(total)
            if total_f >= window.cap_usd * 0.8:
                log.warning(
                    "budget.approaching_cap",
                    provider=provider,
                    window=window.label,
                    spent_usd=total_f,
                    cap_usd=window.cap_usd,
                    pct=round(100 * total_f / window.cap_usd, 1),
                )

    async def status(self) -> dict[str, dict[str, float]]:
        """Current spend per window, in dollars — for /health and /metrics-adjacent reporting."""
        if not self.enabled or self._client is None:
            return {}
        now = time.time()
        out: dict[str, dict[str, float]] = {}
        for window in self._windows:
            try:
                raw = await self._client.get(window.key_fn(now))
            except RedisError:
                out[window.label] = {"spent_usd": -1.0, "cap_usd": window.cap_usd}
                continue
            spent = float(raw) if raw is not None else 0.0
            out[window.label] = {"spent_usd": round(spent, 4), "cap_usd": window.cap_usd}
        return out
