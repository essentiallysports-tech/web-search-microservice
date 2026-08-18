"""Domain exclusion for search results.

Some hosts rank well on Google and are worthless to THIS service specifically.
Not as an editorial opinion — as a measured property of what the extractor can
do with them:

    reddit.com      robots-disallowed          ->     0 chars
    facebook.com    robots-disallowed          ->     0 chars
    instagram.com   JS shell, caption only     ->   145-491 chars
    youtube.com     page furniture, not prose  -> 3k-106k chars of navigation

The last one is the dangerous case rather than the merely useless one. A caller
verifying a claim against "fetched document text" gets 21k characters from a
YouTube watch page, none of which is the story, and a wall of loosely related
entity names is exactly the input most likely to make a claim look supported.

So the default list below is not a taste judgement about social media. It is the
set of hosts that cost a result slot — and sometimes a paid extraction — while
being unable to return the thing this service exists to return.

Two mechanisms, because one is not enough:

- Query-side `-site:` operators (Google syntax, Serper only). Filtering at the
  INDEX means the excluded slots are backfilled with real results, so a request
  for 5 still yields 5. This does the real work.
- Post-filtering by hostname, on every provider. Brave has no equivalent
  operator, and Google's own is not airtight, so this is the backstop that makes
  the guarantee actually hold.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: Hosts excluded unless a deployment or caller says otherwise. See the module
#: docstring for why each is here — every one of them was measured returning
#: nothing usable, not merely judged off-topic.
DEFAULT_BLOCKED_DOMAINS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "reddit.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "pinterest.com",
    "threads.net",
    "threads.com",
    "snapchat.com",
)

#: Google accepts a bounded number of terms per query (~32 words). A long
#: exclusion list would push the real query out of the window, which fails as
#: "no results" rather than as an error. Past this many, the rest of the list is
#: left to the post-filter — degraded backfill, never a wrong result.
MAX_QUERY_OPERATORS = 12

#: A POSITIVE `site:` restriction already narrows the query to one host, so
#: exclusions cannot change the outcome and would only spend query budget.
#: `(?<!-)` keeps this from matching the `-site:` terms we add ourselves.
_SITE_RESTRICTION = re.compile(r"(?:^|\s)(?<!-)site:", re.IGNORECASE)


def parse_domains(raw: str) -> frozenset[str]:
    """Parse a CSV of domains into a normalized set.

    Tolerates the forms people actually paste: a bare host, a scheme, a leading
    `www.`, stray whitespace, a trailing path.
    """
    out: set[str] = set()
    for item in raw.split(","):
        cleaned = normalize_domain(item)
        if cleaned:
            out.add(cleaned)
    return frozenset(out)


def normalize_domain(value: str) -> str:
    """One domain, reduced to a bare lowercase host."""
    cleaned = value.strip().lower()
    if not cleaned:
        return ""
    if "//" in cleaned:
        cleaned = cleaned.split("//", 1)[1]
    cleaned = cleaned.split("/", 1)[0].split("?", 1)[0]
    cleaned = cleaned.removeprefix("www.").strip(".")
    return cleaned


def is_blocked(url: str, blocked: frozenset[str]) -> bool:
    """Whether `url`'s host is excluded.

    Subdomains count: blocking `youtube.com` also blocks `m.youtube.com` and
    `music.youtube.com`, which is the only reading that makes a block list
    useful — otherwise every mobile host has to be listed by hand.
    """
    if not blocked:
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False

    host = host.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in blocked)


def filter_blocked(urls_and_items, blocked: frozenset[str]):
    """Drop items whose URL is blocked. Takes (url, item) pairs, yields items."""
    for url, item in urls_and_items:
        if not is_blocked(url, blocked):
            yield item


def with_exclusions(query: str, blocked: frozenset[str]) -> str:
    """Append Google `-site:` operators to `query`.

    Returns the query unchanged when there is nothing to add, or when it already
    carries a positive `site:` restriction — in that case the caller has already
    narrowed to one host and exclusions cannot change the result.
    """
    if not blocked or _SITE_RESTRICTION.search(query):
        return query

    # Sorted so the same request produces the same query string, which keeps
    # the provider's own caching (and ours) from splitting on term order.
    terms = " ".join(f"-site:{d}" for d in sorted(blocked)[:MAX_QUERY_OPERATORS])
    return f"{query} {terms}" if terms else query


def key_material(blocked: frozenset[str]) -> str:
    """Stable representation for the cache key.

    Exclusions change which results a query returns, so two requests differing
    only in their block list are different queries and must not share an entry.
    """
    return ",".join(sorted(blocked))
