"""User-Agent rotation.

A fixed UA across thousands of requests is one of the easiest crawler fingerprints to
block, and rotating a small pool of current desktop strings costs nothing.

Two rules for the pool:

- Only real, current strings. A made-up or outdated UA is a STRONGER bot signal than
  the default one.
- Matched client hints. `Sec-CH-UA` must agree with the UA it accompanies — a Chrome UA
  with Firefox hints is an obvious mismatch.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    user_agent: str
    #: Client hints that must accompany this UA, or empty for non-Chromium.
    client_hints: dict[str, str]


_CHROME_131_WIN = BrowserProfile(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    client_hints={
        "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    },
)

_CHROME_131_MAC = BrowserProfile(
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    client_hints={
        "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
    },
)

_FIREFOX_133_WIN = BrowserProfile(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    client_hints={},  # Firefox does not send UA client hints
)

_FIREFOX_133_LINUX = BrowserProfile(
    user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    client_hints={},
)

_SAFARI_18_MAC = BrowserProfile(
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
    ),
    client_hints={},
)

PROFILES: tuple[BrowserProfile, ...] = (
    _CHROME_131_WIN,
    _CHROME_131_MAC,
    _FIREFOX_133_WIN,
    _FIREFOX_133_LINUX,
    _SAFARI_18_MAC,
)

DEFAULT_PROFILE = _CHROME_131_WIN


def pick_profile(*, rotate: bool = True) -> BrowserProfile:
    return random.choice(PROFILES) if rotate else DEFAULT_PROFILE


def headers_for(profile: BrowserProfile) -> dict[str, str]:
    """Navigation headers consistent with `profile`.

    The Sec-Fetch-* family is what most gates actually check — a lone User-Agent
    override is often not enough to pass for a browser.
    """
    headers = {
        "User-Agent": profile.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    headers.update(profile.client_hints)
    return headers
