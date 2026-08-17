"""Shared helpers for the stage verification scripts.

These hit real infrastructure — search APIs, Valkey, live websites — so they are kept
out of the pytest suite, which must stay hermetic and fast. Run them by hand when
validating a stage end to end.

THEY COST MONEY. Both search providers are paid and a configured FIRECRAWL_API_KEY means
extraction is billed too, so every stage spends. `spend_notice` announces the estimate up
front and `spend_report` prints actual consumption, so a sweep cannot quietly run up a
bill — but do not put these in a loop.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Scripts are run directly (`python scripts/verify/stage1_search.py`), so the
# repo root has to go on the path before `app` is importable.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Redis is the only local dependency left; search is two external APIs now.
LOCAL_REDIS = "redis://127.0.0.1:6379/0"

# Serper list price at the $50 entry pack, for turning credits into a rough
# currency figure. Deliberately the *worst* rate — a sweep that looks affordable
# at $0.30/1k should also look affordable at the price actually being paid.
USD_PER_CREDIT = 0.001

_checks: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def info(message: str) -> None:
    print(f"  {message}")


def check(label: str, passed: bool, detail: str = "") -> bool:
    """Record a pass/fail without aborting, so one failure doesn't hide the rest."""
    _checks.append((label, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return passed


def summary() -> int:
    """Print the tally and return a process exit code."""
    failed = [c for c in _checks if not c[1]]
    print(f"\n{'-' * 60}")
    print(f"{len(_checks) - len(failed)}/{len(_checks)} checks passed")
    if failed:
        print("\nFAILED:")
        for label, _, detail in failed:
            print(f"  - {label}" + (f" ({detail})" if detail else ""))
        return 1
    print("ALL CHECKS PASSED")
    return 0


class Timer:
    """Context manager that records elapsed milliseconds."""

    def __enter__(self) -> Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.ms = (time.perf_counter() - self._started) * 1000

    @property
    def elapsed_ms(self) -> float:
        return getattr(self, "ms", (time.perf_counter() - self._started) * 1000)


def env_or_dotenv(name: str) -> str | None:
    """Read a key from the process env, falling back to `.env`.

    Benchmark-only credentials (Tavily) aren't Settings fields, so nothing else
    loads them out of `.env` — without this they'd have to be exported by hand.
    """
    value = os.environ.get(name)
    if value:
        return value

    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}=") and not line.startswith("#"):
            candidate = line.split("=", 1)[1].strip()
            # Guard against the trailing-comment-as-value trap that once
            # silently enabled a paid provider.
            if candidate and not candidate.startswith("#"):
                return candidate
    return None


def spend_notice(estimated_queries: int, *, what: str = "search queries") -> None:
    """Announce, before spending it, roughly what this script will cost.

    Not a prompt: the documented way to run a sweep is a single chained command, and
    a blocking confirmation in the middle of it would be worse than useless. The
    point is that the cost is never invisible — `spend_report` then prints what was
    actually consumed.
    """
    usd = estimated_queries * USD_PER_CREDIT
    print(
        f"\n  $ this script makes up to ~{estimated_queries} billable {what} "
        f"(~${usd:.2f} at Serper list price)"
    )


def spend_report() -> None:
    """Print what was actually spent, from the provider's own credit figure."""
    from app.common.metrics import external_calls, search_credits_used

    credits = sum(
        sample.value
        for metric in search_credits_used.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )
    billable = sum(
        sample.value
        for metric in external_calls.collect()
        for sample in metric.samples
        if sample.name.endswith("_total") and sample.labels.get("billable") == "true"
    )
    print(f"\n  $ spent: {credits:.0f} search credits (~${credits * USD_PER_CREDIT:.2f}), "
          f"{billable:.0f} billable provider calls total")


def require_search_key() -> None:
    """Exit early when no search provider is configured.

    Search has no free path now, so an unset key is not a degraded run — it is a
    script that will fail every check for one boring reason.
    """
    from app.config import get_settings

    settings = get_settings()
    if not (settings.serper_api_key or settings.brave_api_key):
        sys.exit(
            "No search provider configured.\n"
            "Set SERPER_API_KEY in .env (primary, ~$1/1k, 2,500 free trial credits "
            "at https://serper.dev) or BRAVE_API_KEY (~$5/1k)."
        )


def preflight(*, need_search_key: bool = False, need_redis: bool = False) -> None:
    """Fail early and clearly when a dependency isn't up."""
    if need_search_key:
        require_search_key()

    if need_redis:
        import asyncio

        from redis.asyncio import Redis

        async def ping() -> bool:
            client = Redis.from_url(LOCAL_REDIS, socket_connect_timeout=2)
            try:
                return bool(await client.ping())
            finally:
                await client.aclose()

        try:
            assert asyncio.run(ping())
        except Exception as exc:
            sys.exit(
                f"Redis/Valkey is not reachable at {LOCAL_REDIS} ({exc!r}).\n"
                "Start it with:\n"
                "  docker compose -f docker-compose.yml -f docker-compose.dev.yml "
                "up -d redis"
            )
