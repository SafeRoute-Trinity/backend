"""
Shared HTTP client utilities for backend services.

Using a process-level AsyncClient with connection pooling avoids creating a new
TCP/TLS connection on every request, which improves latency and throughput
under load.
"""

from __future__ import annotations

import os
from typing import Dict, Tuple

import httpx

_CLIENTS: Dict[Tuple[float, int, int, float], httpx.AsyncClient] = {}


def _pool_limits() -> tuple[int, int, float]:
    max_connections = int(os.getenv("HTTP_CLIENT_MAX_CONNECTIONS", "200"))
    max_keepalive_connections = int(os.getenv("HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS", "50"))
    keepalive_expiry = float(os.getenv("HTTP_CLIENT_KEEPALIVE_EXPIRY_SECONDS", "30"))
    return max_connections, max_keepalive_connections, keepalive_expiry


def get_shared_async_client(timeout_seconds: float) -> httpx.AsyncClient:
    """
    Return a shared AsyncClient keyed by timeout + pool settings.
    """
    max_connections, max_keepalive_connections, keepalive_expiry = _pool_limits()
    key = (
        float(timeout_seconds),
        max_connections,
        max_keepalive_connections,
        keepalive_expiry,
    )
    existing = _CLIENTS.get(key)
    if existing is not None:
        return existing

    client = httpx.AsyncClient(
        timeout=timeout_seconds,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        ),
    )
    _CLIENTS[key] = client
    return client


async def close_shared_async_clients() -> None:
    """
    Close all shared clients for clean service shutdown.
    """
    if not _CLIENTS:
        return
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        await client.aclose()
