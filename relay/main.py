from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from relay.auth.middleware import auth_middleware, get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.config import settings
from relay.endpoints.character import router as character_router
from relay.endpoints.checks import router as checks_router
from relay.endpoints.combat import router as combat_router
from relay.endpoints.companion import router as companion_router
from relay.endpoints.craft import router as craft_router
from relay.endpoints.dialogue import router as dialogue_router
from relay.endpoints.dice import router as dice_router
from relay.endpoints.faction import router as faction_router
from relay.endpoints.scene import router as scene_router
from relay.endpoints.session import router as session_router
from relay.endpoints.shop import router as shop_router
from relay.endpoints.wallet import router as wallet_router
from relay.logging_config import setup_logging
from relay.middleware.rate_limit import rate_limit_middleware

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

# Starlette: last-added middleware is outermost (runs first on request).
# Auth (outermost) sets request.state.token → rate limiter (inner) keys by player_id.
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=auth_middleware)

app.include_router(character_router)
app.include_router(checks_router)
app.include_router(combat_router)
app.include_router(companion_router)
app.include_router(craft_router)
app.include_router(dialogue_router)
app.include_router(dice_router)
app.include_router(faction_router)
app.include_router(scene_router)
app.include_router(session_router)
app.include_router(shop_router)
app.include_router(wallet_router)


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
