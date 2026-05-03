from __future__ import annotations

import logging

from fastapi import FastAPI

from relay.config import settings
from relay.logging_config import setup_logging

setup_logging(level=settings.log_level)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Inkglass Odyssey Relay",
    version="0.1.0",
    docs_url="/docs" if settings.admin_mode else None,
    redoc_url="/redoc" if settings.admin_mode else None,
)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info(
        "Relay starting",
        extra={
            "environment": settings.environment,
            "admin_mode": settings.admin_mode,
            "database_url": settings.database_url,
        },
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("Relay shutting down")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "inkglass-relay"}
