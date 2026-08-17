"""Shared httpx clients for the whole process.

A client per request throws away connection pooling and TLS session reuse, which is a
large share of outbound latency at this service's fan-out. Built once in the lifespan.

Two clients, not one: page fetches go through the optionally-proxied extraction client,
search APIs through the plain one.
"""

from __future__ import annotations

import httpx

from app.config import Settings

_client: httpx.AsyncClient | None = None
_extraction_client: httpx.AsyncClient | None = None


def _client_kwargs(settings: Settings) -> dict:
    return {
        "http2": True,
        "limits": httpx.Limits(
            max_connections=max(settings.max_concurrency * 4, 40),
            max_keepalive_connections=max(settings.max_concurrency * 2, 20),
            keepalive_expiry=30.0,
        ),
        "timeout": httpx.Timeout(
            connect=5.0,
            read=settings.page_timeout_s,
            write=10.0,
            pool=5.0,
        ),
        "follow_redirects": True,
        "max_redirects": 5,
        "headers": {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
    }


def build_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(**_client_kwargs(settings))


def build_extraction_client(settings: Settings) -> httpx.AsyncClient:
    """Client for outbound page fetches, optionally proxied.

    Separate from the default client so a residential proxy applies to third-party
    fetches only: routing search APIs through it would be pointless (they authenticate
    our key) and expensive (proxy traffic is billed per GB).

    Returns the shared client when no proxy is configured, so the common case costs
    nothing extra.
    """
    if not (settings.proxy_url and settings.proxy_for_extraction):
        return get_client()

    return httpx.AsyncClient(**_client_kwargs(settings), proxy=settings.proxy_url)


def set_client(client: httpx.AsyncClient) -> None:
    global _client
    _client = client


def set_extraction_client(client: httpx.AsyncClient) -> None:
    global _extraction_client
    _extraction_client = client


def get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client not initialized; app lifespan did not run")
    return _client


def get_extraction_client() -> httpx.AsyncClient:
    return _extraction_client if _extraction_client is not None else get_client()


async def close_client() -> None:
    global _client, _extraction_client
    if _extraction_client is not None and _extraction_client is not _client:
        await _extraction_client.aclose()
    _extraction_client = None
    if _client is not None:
        await _client.aclose()
        _client = None
