from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload, decode_token

logger = logging.getLogger(__name__)

# Paths that skip token validation entirely.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


async def auth_middleware(request: Request, call_next):
    """Starlette middleware: validates JWT on every non-public request."""
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    client_ip = request.client.host if request.client else "unknown"
    if not auth_header.startswith("Bearer "):
        logger.warning(
            "Auth failed: missing or malformed header",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        return _unauthorized("Missing or malformed Authorization header")

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        payload = decode_token(token)
        request.state.token = payload
    except jwt.ExpiredSignatureError:
        logger.warning(
            "Auth failed: expired token",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        return _unauthorized("Token has expired")
    except jwt.PyJWTError:
        logger.warning(
            "Auth failed: invalid token",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        return _unauthorized("Invalid token")

    return await call_next(request)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"code": "unauthorized", "message": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# FastAPI dependency — reads payload already validated by middleware
# ---------------------------------------------------------------------------


async def get_current_token(
    request: Request,
) -> AccountTokenPayload | SessionTokenPayload:
    """FastAPI Depends() — returns the token payload set by auth_middleware.

    The middleware has already decoded and validated the JWT; this dependency
    is a thin accessor that avoids decoding the token a second time.
    """
    token = getattr(request.state, "token", None)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Missing Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def require_session_token(
    token: Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)],
) -> SessionTokenPayload:
    """Dependency that additionally enforces the token is a session token."""
    if not isinstance(token, SessionTokenPayload):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "A session token is required for this endpoint"},
        )
    return token


def require_tier(min_tier: int) -> Callable:
    """Dependency factory: enforces the caller's tier >= min_tier."""

    async def _check(
        token: Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)],
    ) -> AccountTokenPayload | SessionTokenPayload:
        if token.tier < min_tier:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "forbidden",
                    "message": f"Tier {min_tier} access required",
                },
            )
        return token

    return _check


def require_role(role: str) -> Callable:
    """Dependency factory: enforces the session token has the specified role."""

    async def _check(
        token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    ) -> SessionTokenPayload:
        if token.role != role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "forbidden",
                    "message": f"Role '{role}' is required for this endpoint",
                },
            )
        return token

    return _check
