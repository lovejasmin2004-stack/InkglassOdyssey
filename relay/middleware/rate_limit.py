"""Token-bucket rate limiter for HTTP endpoints (Invariant #13).

Keyed by player_id (from decoded JWT) when available, falling back to
client IP for unauthenticated requests. Stale buckets are evicted lazily
to prevent unbounded memory growth.

WebSocket rate limiting remains inline in the dialogue handler since it
operates per-message within a connection.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

_DEFAULT_RPM = 60
_WINDOW = 60.0
_EVICTION_THRESHOLD = 500
_STALE_SECONDS = 600.0  # 10 minutes


class _Bucket:
    __slots__ = ("last_refill", "tokens")

    def __init__(self) -> None:
        self.tokens: float = _DEFAULT_RPM
        self.last_refill: float = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(_DEFAULT_RPM, self.tokens + elapsed * (_DEFAULT_RPM / _WINDOW))
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


_buckets: dict[str, _Bucket] = {}


def clear_buckets() -> None:
    """Reset all rate limit state. Used by tests."""
    _buckets.clear()


def _evict_stale() -> None:
    """Remove buckets not accessed in _STALE_SECONDS. Called lazily."""
    now = time.monotonic()
    stale_keys = [key for key, bucket in _buckets.items() if (now - bucket.last_refill) > _STALE_SECONDS]
    for key in stale_keys:
        del _buckets[key]


def _get_rate_limit_key(request: Request) -> str:
    """Derive rate limit key: player_id if authenticated, else client IP."""
    token = getattr(request.state, "token", None)
    if token is not None:
        return f"player:{token.player_id}"
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in _EXEMPT_PATHS:
        return await call_next(request)

    if len(_buckets) > _EVICTION_THRESHOLD:
        _evict_stale()

    key = _get_rate_limit_key(request)
    bucket = _buckets.setdefault(key, _Bucket())

    if not bucket.allow():
        logger.warning(
            "Rate limited",
            extra={"rate_limit_key": key, "path": request.url.path},
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"code": "rate_limited", "message": "Too many requests. Please slow down."},
        )

    return await call_next(request)
