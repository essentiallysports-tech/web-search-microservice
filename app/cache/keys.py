"""Cache key construction.

Key design decides the hit rate, and the hit rate decides the bill: exact-string
keys miss on near-duplicates ("best CRM 2026", "Best CRM 2026?", "best crm  2026").

Two levels of query normalization:

- `normalize_query` — casefold, NFKC, collapse whitespace, drop edge punctuation.
  Order-preserving and always safe.
- `canonical_query` — also drops stopwords and sorts tokens. Materially better hit
  rate, but merges queries differing only in word order ("dog bites man" / "man
  bites dog"). Gated by CACHE_AGGRESSIVE_QUERY_KEY.

URL canonicalization is for keys only — the original URL is always what gets fetched.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WS = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^[^\w(\[\"']+|[^\w)\]\"'%]+$")

# Deliberately small: a large list starts merging queries whose meaning lives in the
# small words ("to be or not to be").
_STOPWORDS = frozenset(
    """
    a an the and or of for to in on at by with from is are was were be been
    do does did what which who whom how when where why
    """.split()
)

# Params that never change the document served. Stripping them merges the many
# tagged variants of one article onto one page-cache entry.
_TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mc_", "hsa_", "_hs", "vero_", "wt_")
_TRACKING_PARAMS = frozenset(
    """
    fbclid gclid dclid gbraid wbraid msclkid twclid ttclid igshid yclid
    ref ref_src ref_url referrer source cmpid campaign_id spm scm
    mkt_tok trk trkCampaign sc_channel sc_campaign icid ncid
    _ga _gl gclsrc at_medium at_campaign
    """.split()
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_query(query: str) -> str:
    """Casefold + whitespace/punctuation cleanup. Order preserving."""
    q = unicodedata.normalize("NFKC", query).casefold().strip()
    q = _WS.sub(" ", q)
    return _EDGE_PUNCT.sub("", q).strip()


def canonical_query(query: str) -> str:
    """Stopword-stripped, token-sorted form. Higher hit rate, order-insensitive."""
    normalized = normalize_query(query)
    tokens = [t for t in normalized.split(" ") if t and t not in _STOPWORDS]
    if not tokens:
        # Query was nothing but stopwords — fall back rather than key on "".
        return normalized
    return " ".join(sorted(tokens))


def canonical_url(url: str) -> str:
    """Strip fragments, tracking params, default ports and `www.`.

    Scheme is preserved so the result stays a usable URL; keys fold http/https
    together separately in `_url_key_material`.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip()

    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if port and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query = urlencode(sorted(kept), doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PARAM_PREFIXES)


def _digest(value: str) -> str:
    # blake2b beats sha256 here, and 16 bytes is ample for a cache keyspace.
    return hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest()


def search_key(
    query: str,
    *,
    count: int,
    lang: str,
    freshness: str,
    version: str = "v1",
    aggressive: bool = True,
    exclude: str = "",
) -> str:
    """Key for a search result set.

    `count` is part of the key because a cached 5-result set cannot serve a 20-result
    request. For maximum reuse, request a consistent count and slice locally.

    `exclude` is the caller's domain block list (see search/domains.key_material).
    It belongs in the key because it changes which results come back: serving a
    filtered set to an unfiltered request would silently withhold results the
    caller asked for, and the reverse would return hosts they excluded.
    """
    q = canonical_query(query) if aggressive else normalize_query(query)
    payload = f"{q}|{count}|{lang.lower()}|{freshness.lower()}"
    if exclude:
        # Appended only when non-empty, so existing unfiltered keys keep their
        # shape and a deployment that turns filtering off does not cold-start.
        payload = f"{payload}|x={exclude}"
    return f"wss:{version}:search:{_digest(payload)}"


def _url_key_material(url: str) -> str:
    """Canonical URL with the scheme dropped.

    http:// and https:// serve the same document on essentially every site still
    answering on port 80, and we follow redirects anyway, so folding them onto one
    key is free hits.
    """
    canonical = canonical_url(url)
    _, _, remainder = canonical.partition("://")
    return remainder or canonical


def page_key(url: str, *, version: str = "v1") -> str:
    return f"wss:{version}:page:{_digest(_url_key_material(url))}"


def failure_key(url: str, *, version: str = "v1") -> str:
    return f"wss:{version}:fail:{_digest(_url_key_material(url))}"


def lock_key(key: str) -> str:
    return f"{key}:lock"
