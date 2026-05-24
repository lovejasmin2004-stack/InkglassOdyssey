"""Token-bucket rate limiter for HTTP endpoints (Invariant #13).

Keyed by player_id (from decoded JWT) when available, falling back to
client IP for unauthenticated requests. Stale buckets are evicted lazily
to prevent unbounded memory growth.

WebSocket rate limiting remains inline in the dialogue handler since it
operates per-message within a connection.
"""

from __future__ import annotations

import asyncio
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

_EXEMPT_PREFIXES: tuple[str, ...] = ("/docs", "/redoc")

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
_buckets_lock = asyncio.Lock()


def clear_buckets() -> None:
    """Reset all rate limit state. Used by tests."""
    _buckets.clear()


def _find_stale_keys() -> list[str]:
    """Identify stale bucket keys (can be called under lock — O(n) read only)."""
    now = time.monotonic()
    return [key for key, bucket in _buckets.items() if (now - bucket.last_refill) > _STALE_SECONDS]


def _evict_stale() -> None:
    """Remove buckets not accessed in _STALE_SECONDS. Called under lock.

    Legacy entry point kept for test compatibility.
    """
    for key in _find_stale_keys():
        del _buckets[key]


def _get_rate_limit_key(request: Request) -> str:
    """Derive rate limit key: player_id if authenticated, else client IP."""
    token = getattr(request.state, "token", None)
    if token is not None:
        return f"player:{token.player_id}"
    return request.client.host if request.client else "unknown"


def _is_exempt(path: str) -> bool:
    """Check if a path is exempt from rate limiting."""
    normalized = path.rstrip("/") or "/"
    if normalized in _EXEMPT_PATHS:
        return True
    return normalized.startswith(_EXEMPT_PREFIXES)


async def rate_limit_middleware(request: Request, call_next):
    if _is_exempt(request.url.path):
        return await call_next(request)

    key = _get_rate_limit_key(request)

    # Fast path: check bucket and allow/deny under a short lock hold.
    # Eviction is deferred to minimize lock contention.
    stale_keys: list[str] = []
    async with _buckets_lock:
        if len(_buckets) > _EVICTION_THRESHOLD:
            # Snapshot stale keys (O(n) read) but defer deletion
            stale_keys = _find_stale_keys()

        bucket = _buckets.setdefault(key, _Bucket())
        allowed = bucket.allow()

    # Evict stale buckets outside the hot path (non-blocking for other requests
    # that arrived between the two lock acquisitions).
    if stale_keys:
        async with _buckets_lock:
            for sk in stale_keys:
                _buckets.pop(sk, None)

    if not allowed:
        logger.warning(
            "Rate limited",
            extra={"rate_limit_key": key, "path": request.url.path},
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"code": "rate_limited", "message": "Too many requests. Please slow down."},
        )

    return await call_next(request)
