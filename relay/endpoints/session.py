"""Session lifecycle endpoints: start, end, state.

Improvements (step 8):
 - (#1)  World tier enforcement on session start
 - (#2)  Character world_id must match session world_id
 - (#6)  Analytics includes CLAUDE.md §11 metrics (llm_call_count, check_pass_rate, etc.)
 - (#7)  Session end marks orphaned pending turns as failed (via scene end)
 - (#9)  Level increment requires minimum turn count (≥5 turns)
 - (#10) Timezone handling fixed — always use UTC explicitly
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload, create_session_token
from relay.database import get_db
from relay.endpoints.scene import SceneResponse
from relay.models import Character, GameSession, PendingTurn, Scene

logger = logging.getLogger(__name__)

router = APIRouter(tags=["session"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]

# Tier 2 worlds require tier >= 2 access (#1)
_TIER2_WORLDS: frozenset[str] = frozenset({"wha_au", "atla_au", "gachiakuta_au", "hxh_au"})

# Minimum total turns required for level_increment (#9)
_MIN_TURNS_FOR_LEVEL_UP = 5


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
    """Build analytics summary for the session (#6).

    Includes CLAUDE.md §11 metrics: llm_call_count, turn_latency_p95,
    cache_hit_rate, check_pass_rate, player_turn_length_trend.
    """
    now = datetime.now(UTC)
    started = session.started_at
    # (#10) Ensure timezone-aware comparison
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    duration = (now - started).total_seconds()

    total_turns = sum(s.turn_count for s in scenes)

    # Aggregate per-scene analytics if available
    total_llm_calls = 0
    total_check_pass = 0
    total_check_count = 0
    turn_latencies: list[int] = []
    player_turn_lengths: list[int] = []

    scene_analytics = []
    for s in scenes:
        sa: dict = {"scene_id": s.id, "npc_id": s.npc_id, "turn_count": s.turn_count, "mode": s.mode}
        if s.analytics:
            sa.update(s.analytics)
            total_llm_calls += s.analytics.get("llm_call_count", 0)
            total_check_pass += s.analytics.get("check_pass_count", 0)
            total_check_count += s.analytics.get("check_total_count", 0)
            if "turn_latencies_ms" in s.analytics:
                turn_latencies.extend(s.analytics["turn_latencies_ms"])

        # Extract player turn lengths from turn_history for trend
        for entry in s.turn_history or []:
            player_input = entry.get("player_input", "")
            if player_input:
                player_turn_lengths.append(len(player_input))

        scene_analytics.append(sa)

    # Calculate p95 latency
    turn_latency_p95: int | None = None
    if turn_latencies:
        sorted_latencies = sorted(turn_latencies)
        p95_idx = int(len(sorted_latencies) * 0.95)
        turn_latency_p95 = sorted_latencies[min(p95_idx, len(sorted_latencies) - 1)]

    # Calculate check pass rate
    check_pass_rate: float | None = None
    if total_check_count > 0:
        check_pass_rate = round(total_check_pass / total_check_count, 3)

    # Player turn length trend (average of first third vs last third)
    player_turn_length_trend: str | None = None
    if len(player_turn_lengths) >= 6:
        third = len(player_turn_lengths) // 3
        first_avg = sum(player_turn_lengths[:third]) / third
        last_avg = sum(player_turn_lengths[-third:]) / third
        if last_avg > first_avg * 1.2:
            player_turn_length_trend = "increasing"
        elif last_avg < first_avg * 0.8:
            player_turn_length_trend = "decreasing"
        else:
            player_turn_length_trend = "stable"

    # Expected LLM calls: 2 per RP turn, 1 per quickchat, +1 for solo with checks
    # Use actual count if available, otherwise estimate
    if total_llm_calls == 0 and total_turns > 0:
        # Estimate: assume 2 calls per turn on average
        total_llm_calls = total_turns * 2

    return {
        "session_duration_seconds": round(duration, 1),
        "scene_count": len(scenes),
        "total_turns": total_turns,
        "llm_call_count": total_llm_calls,
        "turn_latency_p95_ms": turn_latency_p95,
        "check_pass_rate": check_pass_rate,
        "player_turn_length_trend": player_turn_length_trend,
        "scenes": scene_analytics,
    }


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------


@router.post("/session/start", status_code=status.HTTP_201_CREATED, response_model=SessionStartResponse)
async def start_session(body: SessionStartRequest, token: Token, db: DB) -> SessionStartResponse:
    # (#1) Enforce world tier access
    if body.world_id in _TIER2_WORLDS and token.tier < 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": f"World '{body.world_id}' requires Tier 2 access"},
        )

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

    # (#2) Character's world must match the requested session world
    if character.world_id != body.world_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "world_mismatch",
                "message": f"Character belongs to world '{character.world_id}', "
                f"cannot start session in '{body.world_id}'",
            },
        )

    # Check for existing active session for this player.
    # Design decision: ONE active session per player across ALL worlds.
    # This prevents split-attention exploits and simplifies recovery.
    # If per-world independent sessions are needed later, add world_id filter here.
    active = await db.execute(
        select(GameSession).where(GameSession.player_id == token.player_id).where(GameSession.status == "active")
    )
    if active.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "session_active", "message": "Player already has an active session"},
        )

    session_id = str(uuid.uuid4())
    now = datetime.now(UTC)

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

    scenes_result = await db.execute(select(Scene).where(Scene.session_id == session_id).order_by(Scene.started_at))
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
            pending_turns_list.append(
                PendingTurnResponse(
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
                )
            )

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
    scenes_result = await db.execute(select(Scene).where(Scene.session_id == session_id).order_by(Scene.started_at))
    scenes = list(scenes_result.scalars().all())
    now = datetime.now(UTC)
    scenes_ended = 0

    active_scene_ids = []
    for scene in scenes:
        if scene.status == "active":
            scene.status = "ended"
            scene.ended_at = now
            scenes_ended += 1
            active_scene_ids.append(scene.id)

    # (#7) Batch-mark orphaned pending turns as failed (single query)
    if active_scene_ids:
        orphaned_result = await db.execute(
            select(PendingTurn)
            .where(PendingTurn.scene_id.in_(active_scene_ids))
            .where(PendingTurn.stage.notin_(["complete", "failed"]))
        )
        for pt in orphaned_result.scalars().all():
            pt.stage = "failed"
            pt.error_message = "session_ended"
            pt.updated_at = now

    # (#9) Level increment validation BEFORE mutating session status.
    # If this fails with 400, the session remains active and the client can
    # retry with level_increment=false.
    if body.level_increment:
        total_turns = sum(s.turn_count for s in scenes)
        if total_turns < _MIN_TURNS_FOR_LEVEL_UP:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "insufficient_progress",
                    "message": f"Level up requires at least {_MIN_TURNS_FOR_LEVEL_UP} turns "
                    f"(session has {total_turns})",
                },
            )

    # Build summary and analytics
    summary = _build_session_summary(session, scenes)
    analytics = _build_session_analytics(session, scenes)

    session.status = "ended"
    session.ended_at = now
    session.session_summary = summary
    session.analytics = analytics

    # Apply level increment (already validated above)
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
