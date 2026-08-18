"""Publish-date normalization for search results.

Providers report a result's age in whatever form their upstream used, and none of
them is machine-comparable as shipped:

    Serper (Google)   "15 hours ago", "4 days ago", "Aug 15, 2026"
    Brave             "2026-08-17T15:25:28Z" (page_age), "2 days ago" (age)

A consumer gating on recency — "drop anything older than 12 hours" — cannot do
anything with "15 hours ago" except reimplement this function, and a consumer that
calls `Date.parse` on it gets NaN and silently treats a fresh story as undated.
So the boundary normalizes: everything leaves as ISO-8601 UTC, or as None.

None means "no usable date", never "now". Guessing a timestamp would let an
undated result pass a freshness gate it was never shown to satisfy.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: "15 hours ago", "a day ago", "1 month ago". Google writes the unit singular or
#: plural and the count as a numeral or an article.
_RELATIVE = re.compile(
    r"^(?P<count>\d+|an?)\s+(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago$",
    re.IGNORECASE,
)

#: Approximations are fine above a week — nothing gates on "is this 30 or 31 days
#: old", and the alternative is a calendar dependency for no gain.
_UNIT_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2_592_000,  # 30d
    "year": 31_536_000,  # 365d
}

#: Absolute forms Google emits once a result is older than about a week.
_ABSOLUTE_FORMATS = (
    "%b %d, %Y",  # Aug 15, 2026
    "%B %d, %Y",  # August 15, 2026
    "%d %b %Y",  # 15 Aug 2026
    "%d %B %Y",  # 15 August 2026
    "%Y-%m-%d",  # 2026-08-15
    "%m/%d/%Y",  # 08/15/2026
)


def to_iso8601(raw: object, *, now: datetime | None = None) -> str | None:
    """Best-effort ISO-8601 UTC for a provider's date field.

    Returns None for anything unrecognized — an unparseable date is reported as
    absent rather than approximated.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None

    reference = now or datetime.now(UTC)

    # Already a timestamp. `fromisoformat` handles the "Z" suffix from 3.11 on, but
    # normalize it anyway so the behaviour does not depend on the interpreter.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    else:
        # A date-only ISO string parses to midnight naive; treat it as UTC rather
        # than as local time, so the same input cannot mean different instants on
        # two machines.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return _stamp(parsed)

    if (match := _RELATIVE.match(value)) is not None:
        count_raw = match.group("count").lower()
        count = 1 if count_raw in ("a", "an") else int(count_raw)
        seconds = _UNIT_SECONDS[match.group("unit").lower()] * count
        return _stamp(reference - timedelta(seconds=seconds))

    lowered = value.lower()
    if lowered == "yesterday":
        return _stamp(reference - timedelta(days=1))
    if lowered == "today":
        return _stamp(reference)

    for fmt in _ABSOLUTE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return _stamp(parsed.replace(tzinfo=UTC))

    return None


def _stamp(moment: datetime) -> str:
    """UTC, second precision, `Z` suffix — the form `Date.parse` accepts."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
