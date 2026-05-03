from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from relay.auth.middleware import auth_middleware, get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.config import settings
from relay.endpoints.character import router as character_router
from relay.logging_config import setup_logging

setup_logging(level=settings.log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Relay starting",
        extra={
            "environment": settings.environment,
            "admin_mode": settings.admin_mode,
            "database_url": settings.database_url,
        },
    )
    yield
    logger.info("Relay shutting down")


app = FastAPI(
    title="Inkglass Odyssey Relay",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.admin_mode else None,
    redoc_url="/redoc" if settings.admin_mode else None,
)

app.add_middleware(BaseHTTPMiddleware, dispatch=auth_middleware)

app.include_router(character_router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = detail
    else:
        body = {"code": str(exc.status_code), "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"code": "validation_error", "message": str(exc.errors())},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "inkglass-relay"}


@app.get("/me")
async def me(
    token: Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)],
) -> dict:
    """Returns the decoded token payload for the caller. Useful for client auth checks."""
    return token.model_dump(mode="json")
