"""Admin interface — FastAPI app on port 8081.

Serves the RP Tester and Library Workshop.  The Workshop reads/writes
content files with schema validation.  The RP Tester creates throwaway
test sessions and connects to the relay's WebSocket on port 8000.

Run standalone:  python -m relay.admin.app
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from relay.admin.reload import (
    CONTENT_TYPES,
    delete_content,
    list_content,
    list_schemas,
    list_worlds,
    read_content,
    read_schema,
    validate_content,
    write_content,
)
from relay.auth.tokens import create_session_token
from relay.database import AsyncSessionLocal, engine
from relay.models import Account, Base, Character, GameSession, Scene

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title="Inkglass Admin", docs_url="/docs", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schema endpoints
# ---------------------------------------------------------------------------


@app.get("/api/schemas")
async def api_list_schemas() -> list[dict[str, str]]:
    return list_schemas()


@app.get("/api/schemas/{name}")
async def api_get_schema(name: str) -> dict:
    schema = read_schema(name)
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
async def api_list_content(content_type: str, world_id: str) -> list[dict[str, Any]]:
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    return list_content(content_type, world_id)


@app.get("/api/content/{content_type}/{world_id}/{file_id}")
async def api_read_content(content_type: str, world_id: str, file_id: str) -> dict:
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    data = read_content(content_type, world_id, file_id)
    if data is None:
        raise HTTPException(404, f"{content_type}/{world_id}/{file_id} not found")
    return data


@app.put("/api/content/{content_type}/{world_id}/{file_id}")
async def api_write_content(content_type: str, world_id: str, file_id: str, request: Request) -> dict:
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    data = await request.json()
    errors = write_content(content_type, world_id, file_id, data)
    if errors:
        raise HTTPException(422, detail=errors)
    return {"status": "ok", "id": file_id}


@app.post("/api/content/{content_type}/{world_id}/validate")
async def api_validate_content(content_type: str, world_id: str, request: Request) -> dict:
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    data = await request.json()
    errors = validate_content(content_type, data)
    return {"valid": len(errors) == 0, "errors": errors}


@app.delete("/api/content/{content_type}/{world_id}/{file_id}")
async def api_delete_content(content_type: str, world_id: str, file_id: str) -> dict:
    if content_type not in CONTENT_TYPES:
        raise HTTPException(400, f"Unknown content type: {content_type}")
    deleted = delete_content(content_type, world_id, file_id)
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
    scene — then returns a session JWT the browser can use to connect to
    the relay's WebSocket.
    """
    player_id = "admin_test_player"
    character_id = f"admin_test_char_{req.world_id}"

    async with AsyncSessionLocal() as db:
        # Ensure test account exists
        from sqlalchemy import select

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

    token = create_session_token(
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
