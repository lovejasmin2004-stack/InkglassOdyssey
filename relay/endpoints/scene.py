"""Scene lifecycle endpoints: start, get, end, patch.

Improvements (step 8):
 - (#3)  Proper Pydantic request model for POST /scene
 - (#4)  NPC existence validated at scene creation time
 - (#5)  Max active scenes per session (cap at 20)
 - (#7)  Scene end marks orphaned pending turns as failed
 - (#8)  Scene summary includes narrative content from turn history
 - (#11) PATCH /scene/{id} for DM/admin scene_state updates
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.ai.npc_loader import NpcLoadError, load_npc
from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.config import settings
from relay.database import get_db
from relay.models import GameSession, PendingTurn, Scene
from relay.persistence.pending_turns import VALID_ENVIRONMENT_EFFECTS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scene"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]

# Maximum number of active scenes per session (#5)
_MAX_ACTIVE_SCENES_PER_SESSION = 20


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SceneStartRequest(BaseModel):
    """Request body for POST /scene (#3)."""

    session_id: str
    npc_id: str
    mode: str = Field(default="rp", pattern="^(rp|quickchat)$")

    model_config = {"extra": "forbid"}


class ScenePatchRequest(BaseModel):
    """Request body for PATCH /scene/{id} (#11).

    Only DM or admin can use this endpoint to inject scene_state changes.
    """

    scene_state: dict | None = None
    hidden_elements: list[dict] | None = None
    environmental_effects: list[str] | None = None

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
    updated_at: datetime
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


def _assert_dm_or_admin(token: AccountTokenPayload | SessionTokenPayload) -> None:
    """Ensure the caller has DM or admin privileges (#11)."""
    is_dm = isinstance(token, SessionTokenPayload) and token.role == "dm"
    if not is_dm and not settings.admin_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Only DMs or admins can modify scene state directly"},
        )


def _build_scene_summary(scene: Scene) -> str:
    """Generate a meaningful scene summary from turn history (#8)."""
    turns = scene.turn_history or []
    if not turns:
        return f"Scene with {scene.npc_id}: no turns recorded."

    # Collect key data points
    check_outcomes = []
    topics = []

    for entry in turns[-6:]:  # Focus on last 6 turns for recency
        player_input = entry.get("player_input", "")
        if player_input:
            # Extract first 60 chars as topic indicator
            topic = player_input[:60].strip()
            if topic:
                topics.append(topic)
        checks = entry.get("checks", [])
        for check in checks:
            skill = check.get("skill", "")
            passed = check.get("passed", False)
            if skill:
                check_outcomes.append(f"{skill} {'passed' if passed else 'failed'}")

    parts = [f"Scene with {scene.npc_id}: {scene.turn_count} turn(s) in {scene.mode} mode."]

    if topics:
        # Show last 3 topics discussed
        recent = topics[-3:]
        parts.append("Topics: " + "; ".join(f'"{t}..."' if len(t) >= 58 else f'"{t}"' for t in recent))

    if check_outcomes:
        parts.append(f"Checks: {', '.join(check_outcomes[-4:])}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/scene", status_code=status.HTTP_201_CREATED, response_model=SceneResponse)
async def start_scene(
    body: SceneStartRequest,  # (#3) Typed request model
    token: Token,
    db: DB,
) -> SceneResponse:
    session_id = body.session_id
    npc_id = body.npc_id
    mode = body.mode

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

    # (#5) Check max active scenes per session
    active_count_result = await db.execute(
        select(func.count()).select_from(Scene).where(Scene.session_id == session_id).where(Scene.status == "active")
    )
    active_count = active_count_result.scalar() or 0
    if active_count >= _MAX_ACTIVE_SCENES_PER_SESSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "scene_limit_reached",
                "message": f"Maximum of {_MAX_ACTIVE_SCENES_PER_SESSION} active scenes per session",
            },
        )

    # (#4) Validate NPC exists for this world
    try:
        npc = load_npc(npc_id, world_id=session.world_id)
    except NpcLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "npc_load_error", "message": f"NPC '{npc_id}' exists but failed to load: {exc}"},
        ) from exc
    if npc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"NPC '{npc_id}' not found in world '{session.world_id}'"},
        )

    scene = Scene(
        id=str(uuid.uuid4()),
        session_id=session_id,
        npc_id=npc_id,
        mode=mode,
        status="active",
        scene_state={"emotional_temperature": 0.5, "environmental_effects": []},
        turn_history=[],
        turn_count=0,
        started_at=datetime.now(UTC),
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

    now = datetime.now(UTC)
    scene.status = "ended"
    scene.ended_at = now

    # (#8) Generate meaningful scene summary from turn history
    scene.scene_summary = _build_scene_summary(scene)

    # (#7) Mark orphaned pending turns as failed
    orphaned_result = await db.execute(
        select(PendingTurn)
        .where(PendingTurn.scene_id == scene_id)
        .where(PendingTurn.stage.notin_(["complete", "failed"]))
    )
    orphaned_turns = list(orphaned_result.scalars().all())
    for pt in orphaned_turns:
        pt.stage = "failed"
        pt.error_message = "scene_ended"
        pt.updated_at = now

    if orphaned_turns:
        logger.info(
            "Orphaned pending turns marked failed on scene end",
            extra={"scene_id": scene_id, "count": len(orphaned_turns)},
        )

    await db.flush()
    await db.refresh(scene)

    logger.info(
        "Scene ended",
        extra={"scene_id": scene_id, "npc_id": scene.npc_id, "turn_count": scene.turn_count},
    )
    return SceneResponse.model_validate(scene)


@router.patch("/scene/{scene_id}", response_model=SceneResponse)
async def patch_scene(scene_id: str, body: ScenePatchRequest, token: Token, db: DB) -> SceneResponse:
    """Update scene_state directly — DM/admin only (#11).

    This enables DMs to inject hidden_elements, environmental effects,
    or arbitrary scene_state fields for narrative control.
    """
    _assert_dm_or_admin(token)

    result = await db.execute(select(Scene).where(Scene.id == scene_id))
    scene = result.scalar_one_or_none()

    if scene is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Scene not found"},
        )

    sess_result = await db.execute(select(GameSession).where(GameSession.id == scene.session_id))
    session = sess_result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Parent session not found"},
        )

    if scene.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "scene_ended", "message": "Cannot modify an ended scene"},
        )

    current_state = dict(scene.scene_state or {})

    # Merge scene_state if provided (shallow merge)
    if body.scene_state is not None:
        current_state.update(body.scene_state)

    # Set hidden_elements if provided
    if body.hidden_elements is not None:
        current_state["hidden_elements"] = body.hidden_elements

    # Set/validate environmental effects if provided
    if body.environmental_effects is not None:
        validated_effects = [e for e in body.environmental_effects if e in VALID_ENVIRONMENT_EFFECTS]
        if len(validated_effects) != len(body.environmental_effects):
            invalid = set(body.environmental_effects) - VALID_ENVIRONMENT_EFFECTS
            logger.warning(
                "Invalid environmental effects rejected in PATCH",
                extra={"scene_id": scene_id, "invalid": list(invalid)},
            )
        current_state["environmental_effects"] = sorted(validated_effects)

    scene.scene_state = current_state
    scene.updated_at = datetime.now(UTC)

    await db.flush()
    await db.refresh(scene)

    logger.info(
        "Scene state patched",
        extra={"scene_id": scene_id, "by": token.player_id},
    )
    return SceneResponse.model_validate(scene)
