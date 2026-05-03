"""Scene lifecycle endpoints: start, get, end."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.models import GameSession, Scene

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scene"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class SceneResponse(BaseModel):
    id: str
    session_id: str
    npc_id: str
    mode: str
    status: str
    scene_state: dict
    turn_count: int
    scene_summary: str | None
    started_at: datetime
    ended_at: datetime | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_scene_owner(session: GameSession | None, token: AccountTokenPayload | SessionTokenPayload) -> None:
    if session is None or session.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Scene belongs to another player"},
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/scene", status_code=status.HTTP_201_CREATED, response_model=SceneResponse)
async def start_scene(
    body: dict,
    token: Token,
    db: DB,
) -> SceneResponse:
    session_id = body.get("session_id")
    npc_id = body.get("npc_id")
    mode = body.get("mode", "rp")

    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_field", "message": "session_id is required"},
        )
    if not npc_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_field", "message": "npc_id is required"},
        )
    if mode not in ("rp", "quickchat"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_field", "message": "mode must be 'rp' or 'quickchat'"},
        )

    result = await db.execute(select(GameSession).where(GameSession.id == session_id))
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Session not found"},
        )
    _assert_scene_owner(session, token)
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_ended", "message": "Session has already ended"},
        )

    scene = Scene(
        id=str(uuid.uuid4()),
        session_id=session_id,
        npc_id=npc_id,
        mode=mode,
        status="active",
        scene_state={"emotional_temperature": 0.0},
        turn_history=[],
        turn_count=0,
        started_at=datetime.now(timezone.utc),
    )
    db.add(scene)
    await db.flush()
    await db.refresh(scene)

    logger.info(
        "Scene started",
        extra={"scene_id": scene.id, "session_id": session_id, "npc_id": npc_id, "mode": mode},
    )
    return SceneResponse.model_validate(scene)


@router.get("/scene/{scene_id}", response_model=SceneResponse)
async def get_scene(scene_id: str, token: Token, db: DB) -> SceneResponse:
    result = await db.execute(select(Scene).where(Scene.id == scene_id))
    scene = result.scalar_one_or_none()

    if scene is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Scene not found"},
        )

    sess_result = await db.execute(select(GameSession).where(GameSession.id == scene.session_id))
    _assert_scene_owner(sess_result.scalar_one_or_none(), token)

    return SceneResponse.model_validate(scene)


@router.post("/scene/{scene_id}/end", response_model=SceneResponse)
async def end_scene(scene_id: str, token: Token, db: DB) -> SceneResponse:
    result = await db.execute(select(Scene).where(Scene.id == scene_id))
    scene = result.scalar_one_or_none()

    if scene is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Scene not found"},
        )

    sess_result = await db.execute(select(GameSession).where(GameSession.id == scene.session_id))
    _assert_scene_owner(sess_result.scalar_one_or_none(), token)

    if scene.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "scene_ended", "message": "Scene has already ended"},
        )

    now = datetime.now(timezone.utc)
    scene.status = "ended"
    scene.ended_at = now

    turns = scene.turn_history or []
    if turns:
        scene.scene_summary = (
            f"Scene with {scene.npc_id}: {scene.turn_count} turn(s) in {scene.mode} mode."
        )
    else:
        scene.scene_summary = f"Scene with {scene.npc_id}: no turns recorded."

    await db.flush()
    await db.refresh(scene)

    logger.info(
        "Scene ended",
        extra={"scene_id": scene_id, "npc_id": scene.npc_id, "turn_count": scene.turn_count},
    )
    return SceneResponse.model_validate(scene)
