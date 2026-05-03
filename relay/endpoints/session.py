"""Session lifecycle endpoints: start, end, state."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload, create_session_token
from relay.database import get_db
from relay.models import Character, GameSession, PendingTurn, Scene

logger = logging.getLogger(__name__)

router = APIRouter(tags=["session"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SessionStartRequest(BaseModel):
    character_id: str
    world_id: str
    mode: str = Field(default="solo", pattern="^(solo|multiplayer)$")
    role: str = Field(default="player", pattern="^(player|dm)$")

    model_config = {"extra": "forbid"}


class SessionStartResponse(BaseModel):
    session_id: str
    session_token: str
    world_id: str
    mode: str
    role: str
    started_at: datetime

    model_config = {"from_attributes": True}


class SessionEndRequest(BaseModel):
    level_increment: bool = False

    model_config = {"extra": "forbid"}


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


class PendingTurnResponse(BaseModel):
    turn_id: str
    scene_id: str
    npc_id: str
    turn_type: str
    stage: str
    player_input: str
    check_results: list | None
    animation_directives: list | None
    scene_changes: dict | None
    final_response: str | None
    created_at: datetime | None


class SessionStateResponse(BaseModel):
    session_id: str
    player_id: str
    character_id: str
    world_id: str
    mode: str
    role: str
    status: str
    session_summary: str | None
    analytics: dict | None
    started_at: datetime
    ended_at: datetime | None
    scenes: list[SceneResponse]
    pending_turns: list[PendingTurnResponse] = []


class SessionEndResponse(BaseModel):
    session_id: str
    status: str
    session_summary: str
    analytics: dict
    ended_at: datetime
    scenes_ended: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_session_owner(token: AccountTokenPayload | SessionTokenPayload, session: GameSession) -> None:
    if session.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Session belongs to another player"},
        )


def _assert_session_active(session: GameSession) -> None:
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_ended", "message": "Session has already ended"},
        )


def _ensure_utc(dt: datetime) -> datetime:
    """Guarantee a timezone-aware UTC datetime.

    SQLite strips tzinfo on storage; this restores it so arithmetic
    against datetime.now(timezone.utc) is safe.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _build_session_summary(session: GameSession, scenes: list[Scene]) -> str:
    """Generate a summary of the session from scene data."""
    total_turns = sum(s.turn_count for s in scenes)
    npc_ids = list({s.npc_id for s in scenes})
    scene_summaries = [s.scene_summary for s in scenes if s.scene_summary]

    parts = [f"Session in {session.world_id}."]
    parts.append(f"{len(scenes)} scene(s), {total_turns} total turn(s).")

    if npc_ids:
        parts.append(f"NPCs encountered: {', '.join(npc_ids)}.")

    if scene_summaries:
        parts.append("Scene highlights: " + " ".join(scene_summaries))

    return " ".join(parts)


def _build_session_analytics(session: GameSession, scenes: list[Scene]) -> dict:
    """Build analytics summary for the session."""
    now = datetime.now(timezone.utc)
    duration = (now - _ensure_utc(session.started_at)).total_seconds()

    total_turns = sum(s.turn_count for s in scenes)
    scene_analytics = []
    for s in scenes:
        sa = {"scene_id": s.id, "npc_id": s.npc_id, "turn_count": s.turn_count, "mode": s.mode}
        if s.analytics:
            sa.update(s.analytics)
        scene_analytics.append(sa)

    return {
        "session_duration_seconds": round(duration, 1),
        "scene_count": len(scenes),
        "total_turns": total_turns,
        "scenes": scene_analytics,
    }


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@router.post("/session/start", status_code=status.HTTP_201_CREATED, response_model=SessionStartResponse)
async def start_session(body: SessionStartRequest, token: Token, db: DB) -> SessionStartResponse:
    # Verify character exists and belongs to the player
    result = await db.execute(select(Character).where(Character.id == body.character_id))
    character = result.scalar_one_or_none()

    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    if character.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Character belongs to another player"},
        )

    # Check for existing active session for this player
    active = await db.execute(
        select(GameSession)
        .where(GameSession.player_id == token.player_id)
        .where(GameSession.status == "active")
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_active", "message": "Player already has an active session"},
        )

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    session = GameSession(
        id=session_id,
        player_id=token.player_id,
        character_id=body.character_id,
        world_id=body.world_id,
        mode=body.mode,
        role=body.role,
        status="active",
        started_at=now,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)

    session_token = create_session_token(
        player_id=token.player_id,
        world_id=body.world_id,
        session_id=session_id,
        tier=token.tier,
        role=body.role,
        mode=body.mode,
    )

    logger.info(
        "Session started",
        extra={"session_id": session_id, "player_id": token.player_id, "world_id": body.world_id},
    )
    return SessionStartResponse(
        session_id=session.id,
        session_token=session_token,
        world_id=session.world_id,
        mode=session.mode,
        role=session.role,
        started_at=session.started_at,
    )


@router.get("/session/{session_id}/state", response_model=SessionStateResponse)
async def get_session_state(session_id: str, token: Token, db: DB) -> SessionStateResponse:
    result = await db.execute(select(GameSession).where(GameSession.id == session_id))
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Session not found"},
        )
    _assert_session_owner(token, session)

    scenes_result = await db.execute(
        select(Scene).where(Scene.session_id == session_id).order_by(Scene.started_at)
    )
    scenes = list(scenes_result.scalars().all())

    # Collect incomplete pending turns across all scenes in this session
    scene_ids = [s.id for s in scenes]
    pending_turns_list: list[PendingTurnResponse] = []
    if scene_ids:
        pt_result = await db.execute(
            select(PendingTurn)
            .where(PendingTurn.scene_id.in_(scene_ids))
            .where(PendingTurn.stage.notin_(["complete", "failed"]))
            .order_by(PendingTurn.created_at)
        )
        for pt in pt_result.scalars().all():
            pending_turns_list.append(PendingTurnResponse(
                turn_id=pt.id,
                scene_id=pt.scene_id,
                npc_id=pt.npc_id,
                turn_type=pt.turn_type,
                stage=pt.stage,
                player_input=pt.player_input,
                check_results=pt.check_results,
                animation_directives=pt.animation_directives,
                scene_changes=pt.scene_changes,
                final_response=pt.final_response,
                created_at=pt.created_at,
            ))

    return SessionStateResponse(
        session_id=session.id,
        player_id=session.player_id,
        character_id=session.character_id,
        world_id=session.world_id,
        mode=session.mode,
        role=session.role,
        status=session.status,
        session_summary=session.session_summary,
        analytics=session.analytics,
        started_at=session.started_at,
        ended_at=session.ended_at,
        scenes=[SceneResponse.model_validate(s) for s in scenes],
        pending_turns=pending_turns_list,
    )


@router.post("/session/{session_id}/end", response_model=SessionEndResponse)
async def end_session(session_id: str, body: SessionEndRequest, token: Token, db: DB) -> SessionEndResponse:
    result = await db.execute(select(GameSession).where(GameSession.id == session_id))
    session = result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Session not found"},
        )
    _assert_session_owner(token, session)
    _assert_session_active(session)

    # End any still-active scenes
    scenes_result = await db.execute(
        select(Scene).where(Scene.session_id == session_id).order_by(Scene.started_at)
    )
    scenes = list(scenes_result.scalars().all())
    now = datetime.now(timezone.utc)
    scenes_ended = 0

    for scene in scenes:
        if scene.status == "active":
            scene.status = "ended"
            scene.ended_at = now
            scenes_ended += 1

    # Build summary and analytics
    summary = _build_session_summary(session, scenes)
    analytics = _build_session_analytics(session, scenes)

    session.status = "ended"
    session.ended_at = now
    session.session_summary = summary
    session.analytics = analytics

    # Level increment if requested
    if body.level_increment:
        char_result = await db.execute(select(Character).where(Character.id == session.character_id))
        character = char_result.scalar_one_or_none()
        if character and character.level < 20:
            character.level += 1
            character.updated_at = now
            logger.info(
                "Character leveled up",
                extra={"character_id": character.id, "new_level": character.level},
            )

    await db.flush()

    logger.info(
        "Session ended",
        extra={
            "session_id": session_id,
            "player_id": session.player_id,
            "scenes_ended": scenes_ended,
            "total_turns": analytics["total_turns"],
        },
    )
    return SessionEndResponse(
        session_id=session.id,
        status=session.status,
        session_summary=summary,
        analytics=analytics,
        ended_at=session.ended_at,
        scenes_ended=scenes_ended,
    )


# ---------------------------------------------------------------------------
# Scene endpoints
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
    _assert_session_owner(token, session)
    _assert_session_active(session)

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

    # Verify ownership through parent session
    sess_result = await db.execute(select(GameSession).where(GameSession.id == scene.session_id))
    session = sess_result.scalar_one_or_none()
    if session is None or session.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Scene belongs to another player"},
        )

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

    # Verify ownership through parent session
    sess_result = await db.execute(select(GameSession).where(GameSession.id == scene.session_id))
    session = sess_result.scalar_one_or_none()
    if session is None or session.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Scene belongs to another player"},
        )

    if scene.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "scene_ended", "message": "Scene has already ended"},
        )

    now = datetime.now(timezone.utc)
    scene.status = "ended"
    scene.ended_at = now

    # Generate scene summary from turn history
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
