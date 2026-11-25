"""slowapi rate limiter keyed by signed-in user (falls back to client IP)."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings


def rate_key(request: Request) -> str:
    uid = getattr(request.state, "uid", None)
    if uid:
        return f"user:{uid}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=rate_key,
    default_limits=[settings.rate_limit_default],
    storage_uri=settings.rate_limit_storage,
    headers_enabled=False,
)
