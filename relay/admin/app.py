"""Admin interface — FastAPI app on port 8081.

Serves the RP Tester and Library Workshop.  The Workshop reads/writes
content files with schema validation.  The RP Tester creates throwaway
test sessions and connects to the relay's WebSocket on port 8000.

Security:
- Authenticated via ``ADMIN_SECRET`` bearer token (skipped when unset
  in development for convenience — logged as warning).
- CORS locked to localhost.
- Origin-based CSRF protection on mutating requests.
- Request body size capped at 1 MB.
- All path parameters validated against strict regex.

Run standalone:  python -m relay.admin.app
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, select
from starlette.middleware.base import BaseHTTPMiddleware

from relay.admin.reload import (
    CONTENT_TYPES,
    SAFE_ID_RE,
    WORLD_IDS,
    delete_content,
    list_content,
    list_schemas,
    list_worlds,
    read_content,
    read_schema,
    validate_content,
    write_content,
)
from relay.auth.tokens import create_test_session_token
from relay.config import settings
from relay.database import AsyncSessionLocal, engine
from relay.models import Account, Base, Character, GameSession, Scene

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_MAX_BODY_BYTES = 1_048_576  # 1 MB
_ALLOWED_ORIGINS = {"http://127.0.0.1:8081", "http://localhost:8081"}
_SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Paths that skip admin auth (static assets served to browser).
_PUBLIC_PATH_PREFIXES = ("/static/",)
_PUBLIC_PATHS = frozenset({"/", "/docs", "/openapi.json", "/redoc"})


# ---------------------------------------------------------------------------
# Middleware functions
# ---------------------------------------------------------------------------

_admin_secret_warned = False


async def admin_auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Require ``Authorization: Bearer <ADMIN_SECRET>`` on API routes.

    Static assets and the root page are exempt so the browser can load
    the SPA.  In development, if ``ADMIN_SECRET`` is unset the gate is
    skipped with a one-time warning.
    """
    global _admin_secret_warned

    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    secret = settings.admin_secret
    if not secret:
        if not _admin_secret_warned:
            logger.warning(
                "ADMIN_SECRET is not set — admin interface is unprotected. Set ADMIN_SECRET in .env for production."
            )
            _admin_secret_warned = True
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header == f"Bearer {secret}":
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"code": "unauthorized", "message": "Invalid or missing admin secret"},
    )


async def csrf_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Reject mutating requests whose Origin header doesn't match localhost."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        # Origin is absent for same-origin requests in some browsers and for
        # non-browser callers (curl, httpie).  Only reject when present and wrong.
        if origin and origin not in _ALLOWED_ORIGINS:
            return JSONResponse(
                status_code=403,
                content={"code": "csrf_rejected", "message": f"Origin {origin} not allowed"},
            )
    return await call_next(request)


async def body_size_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Reject requests with Content-Length exceeding the limit."""
    cl = request.headers.get("Content-Length")
    if cl is not None:
        try:
            if int(cl) > _MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "code": "payload_too_large",
                        "message": f"Request body exceeds {_MAX_BODY_BYTES} bytes",
                    },
                )
        except ValueError:
            pass
    return await call_next(request)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------


async def _periodic_test_cleanup() -> None:
    """Delete test sessions and scenes older than 1 hour, every 30 minutes."""
    while True:
        await asyncio.sleep(1800)
        try:
            async with AsyncSessionLocal() as db:
                cutoff = datetime.now(UTC) - timedelta(hours=1)
                cutoff_naive = cutoff.replace(tzinfo=None)
                await db.execute(
                    delete(Scene).where(
                        Scene.session_id.like("admin_test_%"),
                        Scene.started_at < cutoff_naive,
                    )
                )
                await db.execute(
                    delete(GameSession).where(
                        GameSession.id.like("admin_test_%"),
                        GameSession.started_at < cutoff_naive,
                    )
                )
                await db.commit()
                logger.debug("Test session cleanup sweep completed")
        except Exception:
            logger.exception("Test session cleanup failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    cleanup_task = asyncio.create_task(_periodic_test_cleanup())
    yield
    cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleanup_task


app = FastAPI(title="Inkglass Admin", docs_url="/docs", lifespan=_lifespan)

# Middleware registration — outermost runs first on request.
# Order: body_size → csrf → auth → CORS → handler
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BaseHTTPMiddleware, dispatch=admin_auth_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=csrf_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=body_size_middleware)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_ids(content_type: str, world_id: str, file_id: str | None = None) -> None:
    """Validate path parameters against strict patterns.

    Prevents path traversal (Fix #2) and unknown world IDs (Fix #6).
    """
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    if world_id not in WORLD_IDS:
        raise HTTPException(400, f"Unknown world: {world_id}")
    if file_id is not None and not SAFE_ID_RE.match(file_id):
        raise HTTPException(400, f"Invalid file_id: {file_id}")


# ---------------------------------------------------------------------------
# Schema endpoints
# ---------------------------------------------------------------------------


@app.get("/api/schemas")
async def api_list_schemas() -> list[dict[str, str]]:
    return await list_schemas()


@app.get("/api/schemas/{name}")
async def api_get_schema(name: str) -> dict:
    if not _SCHEMA_NAME_RE.match(name):
        raise HTTPException(400, f"Invalid schema name: {name}")
    schema = await read_schema(name)
    if schema is None:
        raise HTTPException(404, f"Schema '{name}' not found")
    return schema


# ---------------------------------------------------------------------------
# World / content-type metadata
# ---------------------------------------------------------------------------


@app.get("/api/worlds")
async def api_list_worlds() -> list[str]:
    return list_worlds()


@app.get("/api/content-types")
async def api_list_content_types() -> dict[str, dict[str, Any]]:
    return {k: {"schema": v["schema"]} for k, v in CONTENT_TYPES.items()}


# ---------------------------------------------------------------------------
# Content CRUD
# ---------------------------------------------------------------------------


@app.get("/api/content/{content_type}/{world_id}")
async def api_list_content(
    content_type: str,
    world_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
) -> dict[str, Any]:
    _validate_ids(content_type, world_id)
    items = await list_content(content_type, world_id)
    if search:
        q = search.lower()
        items = [i for i in items if q in (i.get("name", "") + i["id"]).lower()]
    total = len(items)
    return {
        "items": items[offset : offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/content/{content_type}/{world_id}/{file_id}")
async def api_read_content(content_type: str, world_id: str, file_id: str) -> JSONResponse:
    _validate_ids(content_type, world_id, file_id)
    result = await read_content(content_type, world_id, file_id)
    if result is None:
        raise HTTPException(404, f"{content_type}/{world_id}/{file_id} not found")
    return JSONResponse(
        content=result["data"],
        headers={"ETag": f'"{result["etag"]}"'},
    )


@app.put("/api/content/{content_type}/{world_id}/{file_id}")
async def api_write_content(content_type: str, world_id: str, file_id: str, request: Request) -> dict:
    _validate_ids(content_type, world_id, file_id)
    data = await request.json()

    # Optimistic concurrency: client sends ETag from read
    if_match = request.headers.get("If-Match")
    expected_etag: str | None = None
    if if_match:
        expected_etag = if_match.strip('"')

    errors = await write_content(content_type, world_id, file_id, data, expected_etag=expected_etag)
    if errors:
        # Distinguish conflict from validation errors
        if any("Conflict" in e for e in errors):
            raise HTTPException(409, detail=errors)
        raise HTTPException(422, detail=errors)
    return {"status": "ok", "id": file_id}


@app.post("/api/content/{content_type}/{world_id}/validate")
async def api_validate_content(content_type: str, world_id: str, request: Request) -> dict:
    _validate_ids(content_type, world_id)
    data = await request.json()
    errors = await validate_content(content_type, data)
    return {"valid": len(errors) == 0, "errors": errors}


@app.delete("/api/content/{content_type}/{world_id}/{file_id}")
async def api_delete_content(content_type: str, world_id: str, file_id: str) -> dict:
    _validate_ids(content_type, world_id, file_id)
    deleted = await delete_content(content_type, world_id, file_id)
    if not deleted:
        raise HTTPException(404, f"{content_type}/{world_id}/{file_id} not found")
    return {"status": "deleted", "id": file_id}


# ---------------------------------------------------------------------------
# RP Tester — test session bootstrapper
# ---------------------------------------------------------------------------


class TestSessionRequest(BaseModel):
    world_id: str
    npc_id: str
    mode: str = "rp"


@app.post("/api/test-session")
async def api_create_test_session(req: TestSessionRequest) -> dict:
    """Bootstrap a throwaway test session for the RP Tester.

    Creates (or reuses) a test account, test character, game session, and
    scene — then returns a short-lived session JWT (30 min) the browser
    can use to connect to the relay's WebSocket.
    """
    player_id = "admin_test_player"
    character_id = f"admin_test_char_{req.world_id}"

    async with AsyncSessionLocal() as db:
        # Ensure test account exists
        acct = (await db.execute(select(Account).where(Account.id == player_id))).scalar_one_or_none()
        if acct is None:
            acct = Account(
                id=player_id,
                email="admin@test.local",
                password_hash="not-a-real-hash",
                tier=2,
            )
            db.add(acct)
            await db.flush()

        # Ensure test character exists for this world
        char = (await db.execute(select(Character).where(Character.id == character_id))).scalar_one_or_none()
        if char is None:
            char = Character(
                id=character_id,
                player_id=player_id,
                world_id=req.world_id,
                name="Admin Test Character",
                level=5,
                specialisation_path_id="none",
                ability_scores={
                    "strength": 14,
                    "dexterity": 12,
                    "constitution": 13,
                    "intelligence": 10,
                    "wisdom": 15,
                    "charisma": 8,
                },
                skill_proficiencies=["perception", "insight", "athletics"],
                saving_throw_proficiencies=["wisdom", "constitution"],
                hp_max=38,
                hp_current=38,
                ac=14,
            )
            db.add(char)
            await db.flush()

        # Create session
        session_id = f"admin_test_{uuid.uuid4().hex[:12]}"
        session = GameSession(
            id=session_id,
            player_id=player_id,
            character_id=character_id,
            world_id=req.world_id,
            mode="solo",
            role="dm",
            status="active",
        )
        db.add(session)
        await db.flush()

        # Create scene
        scene_id = f"admin_scene_{uuid.uuid4().hex[:12]}"
        scene = Scene(
            id=scene_id,
            session_id=session_id,
            npc_id=req.npc_id,
            mode=req.mode,
            status="active",
            scene_state={},
        )
        db.add(scene)
        await db.commit()

    token = create_test_session_token(
        player_id=player_id,
        world_id=req.world_id,
        session_id=session_id,
        tier=2,
        role="dm",
        mode="solo",
    )

    return {
        "session_token": token,
        "session_id": session_id,
        "scene_id": scene_id,
        "character_id": character_id,
        "npc_id": req.npc_id,
        "world_id": req.world_id,
    }


@app.post("/api/test-sessions/cleanup")
async def api_cleanup_test_sessions() -> dict:
    """Delete test sessions and scenes older than 1 hour."""
    async with AsyncSessionLocal() as db:
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        cutoff_naive = cutoff.replace(tzinfo=None)

        scene_result = await db.execute(
            delete(Scene).where(Scene.session_id.like("admin_test_%"), Scene.started_at < cutoff_naive)
        )
        session_result = await db.execute(
            delete(GameSession).where(GameSession.id.like("admin_test_%"), GameSession.started_at < cutoff_naive)
        )
        await db.commit()

    return {
        "status": "cleaned",
        "scenes_deleted": scene_result.rowcount,
        "sessions_deleted": session_result.rowcount,
    }


# ---------------------------------------------------------------------------
# Static files and index
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = _STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run("relay.admin.app:app", host="127.0.0.1", port=8081, reload=True)
