"""Token-bucket rate limiter for HTTP endpoints (Invariant #13).

Keyed by client IP. WebSocket rate limiting remains inline in the
dialogue handler since it operates per-message within a connection.
"""
from __future__ import annotations

import logging
import time

from fastapi import Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})

_DEFAULT_RPM = 60
_WINDOW = 60.0


class _Bucket:
    __slots__ = ("tokens", "last_refill")

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


async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in _EXEMPT_PATHS:
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    bucket = _buckets.setdefault(client_ip, _Bucket())

    if not bucket.allow():
        logger.warning(
            "Rate limited",
            extra={"client_ip": client_ip, "path": request.url.path},
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"code": "rate_limited", "message": "Too many requests. Please slow down."},
        )

    return await call_next(request)
