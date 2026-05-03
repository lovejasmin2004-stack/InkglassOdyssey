from __future__ import annotations

import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload, decode_token

logger = logging.getLogger(__name__)

# Paths that skip token validation entirely.
_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})

_bearer = HTTPBearer(auto_error=False)


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


def _unauthorized(detail: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"code": "unauthorized", "message": detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# FastAPI dependency — use on routes that need the decoded payload
# ---------------------------------------------------------------------------

async def get_current_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AccountTokenPayload | SessionTokenPayload:
    """FastAPI Depends() — extracts and validates the bearer token."""
    client_ip = request.client.host if request.client else "unknown"
    if credentials is None:
        logger.warning(
            "Auth failed: no credentials",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Missing Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        logger.warning(
            "Auth failed: expired token",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Token has expired"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        logger.warning(
            "Auth failed: invalid token",
            extra={"path": request.url.path, "client_ip": client_ip},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid token"},
            headers={"WWW-Authenticate": "Bearer"},
        )


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
