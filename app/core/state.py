"""Process-wide singletons (Redis, shared HTTP client, Socket.IO server)."""
from __future__ import annotations

import time

import httpx
import redis.asyncio as aioredis

from app.config import settings

redis: aioredis.Redis = aioredis.from_url(
    settings.redis_url, decode_responses=True, health_check_interval=30, max_connections=200
)

STARTED_AT = time.time()

_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    """Shared outbound HTTP client (no redirects followed implicitly)."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=8.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            headers={"User-Agent": "DeminicAI/1.0 (+self-hosted assistant)"},
            follow_redirects=False,
        )
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None
