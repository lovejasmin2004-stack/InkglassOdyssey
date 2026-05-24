"""Pending turn persistence -- write-ahead for crash recovery.

Each function opens and commits its own DB session so that stage
transitions are durable even if the caller crashes afterwards.
Invariant #12: session state persisted before processing.

Improvements (step 9):
 - (#1)  Stale turn timeout — marks turns older than threshold as failed
 - (#2)  Client-initiated resume — resume_turn() re-enters pipeline from last stage
 - (#3)  Stage transition validation — forward-only transitions enforced
 - (#4)  Retry count / attempt tracking — retry_count + parent_turn_id fields
 - (#5)  Session-scoped recovery — get_pending_turns filters by session_id
 - (#6)  created_at in recovery payload — already in dict, now included in WS frame
 - (#7)  character_snapshot populated — dialogue endpoint passes character data
 - (#8)  check_results persisted on complete_turn — for consistency
 - (#9)  Pytest integration test — test_pending_turns.py covers recovery flow
 - (#10) Idempotency guard on complete_turn — no double-complete
 - (#11) Non-blocking recovery — send_recovery_data is lightweight
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select

import relay.database as _db
from relay.models import PendingTurn, Scene

logger = logging.getLogger(__name__)

STAGES = ("received", "analysis", "checks_resolved", "streaming", "complete", "failed")

# Valid environmental effects that can be added/removed by scene_changes.
VALID_ENVIRONMENT_EFFECTS = frozenset(
    {
        "darkness",
        "difficult_terrain",
        "high_ground",
        "extreme_weather",
        "hazard",
    }
)

# Stale turn threshold in seconds (#1). Turns older than this are auto-failed.
_STALE_TURN_THRESHOLD_SECONDS = 300  # 5 minutes

# Maximum retry attempts for a single turn (#4).
MAX_RETRIES = 3


class StageTransitionError(Exception):
    """Raised when a stage transition violates forward-only rule (#3)."""


@asynccontextmanager
async def _session():
    """Open a short-lived DB session from the canonical factory.

    Uses relay.database.AsyncSessionLocal so that test overrides via
    ``import relay.database; relay.database.AsyncSessionLocal = ...``
    propagate automatically — no separate set_session_factory needed.
    """
    async with _db.AsyncSessionLocal() as session:
        yield session


def _validate_stage_transition(current: str, target: str) -> None:
    """Validate that a stage transition is forward-only (#3).

    Terminal states (complete, failed) can only transition to themselves
    (which is a no-op). Any other backward transition is rejected.
    """
    if current == target:
        return  # No-op, idempotent

    # Failed is a terminal state — nothing transitions out of it
    if current == "failed":
        raise StageTransitionError(f"Cannot transition from terminal state 'failed' to '{target}'")

    # Complete is a terminal state — nothing transitions out of it
    if current == "complete":
        raise StageTransitionError(f"Cannot transition from terminal state 'complete' to '{target}'")

    # Both current and target must be valid stages
    if current not in STAGES:
        raise StageTransitionError(f"Unknown current stage: '{current}'")
    if target not in STAGES:
        raise StageTransitionError(f"Unknown target stage: '{target}'")

    # Forward-only: target must come after current in the tuple
    # Exception: "failed" can be reached from any non-terminal state
    if target == "failed":
        return

    current_idx = STAGES.index(current)
    target_idx = STAGES.index(target)
    if target_idx <= current_idx:
        raise StageTransitionError(f"Backward stage transition not allowed: '{current}' -> '{target}'")


async def create_pending_turn(
    *,
    scene_id: str,
    player_id: str,
    npc_id: str,
    turn_type: str,
    player_input: str,
    character_snapshot: dict | None = None,
    parent_turn_id: str | None = None,
    retry_count: int = 0,
) -> str:
    """Write a pending turn record before any processing begins. Returns turn ID.

    Parameters:
        parent_turn_id: Links this turn to a previous failed attempt (#4).
        retry_count: Number of prior attempts for this logical turn (#4).
    """
    turn_id = f"pt_{uuid.uuid4().hex[:12]}"
    async with _session() as db:
        pt = PendingTurn(
            id=turn_id,
            scene_id=scene_id,
            player_id=player_id,
            npc_id=npc_id,
            turn_type=turn_type,
            stage="received",
            player_input=player_input,
            character_snapshot=character_snapshot,
            parent_turn_id=parent_turn_id,
            retry_count=retry_count,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(pt)
        await db.commit()
    logger.info(
        "Pending turn created",
        extra={"turn_id": turn_id, "scene_id": scene_id, "stage": "received", "retry_count": retry_count},
    )
    return turn_id


async def update_stage(
    turn_id: str,
    stage: str,
    *,
    analysis_result: dict | None = None,
    check_results: list | None = None,
    animation_directives: list | None = None,
    scene_changes: dict | None = None,
    final_response: str | None = None,
    error_message: str | None = None,
) -> None:
    """Advance a pending turn to a new stage, persisting intermediate results.

    Validates forward-only stage transitions (#3). Raises StageTransitionError
    if the transition is invalid.
    """
    async with _session() as db:
        result = await db.execute(select(PendingTurn).where(PendingTurn.id == turn_id))
        pt = result.scalar_one_or_none()
        if pt is None:
            logger.error("Pending turn not found for stage update", extra={"turn_id": turn_id})
            return

        # (#3) Validate forward-only transition
        _validate_stage_transition(pt.stage, stage)

        pt.stage = stage
        pt.updated_at = datetime.now(UTC)

        if analysis_result is not None:
            pt.analysis_result = analysis_result
        if check_results is not None:
            pt.check_results = check_results
        if animation_directives is not None:
            pt.animation_directives = animation_directives
        if scene_changes is not None:
            pt.scene_changes = scene_changes
        if final_response is not None:
            pt.final_response = final_response
        if error_message is not None:
            pt.error_message = error_message

        await db.commit()
    logger.info("Pending turn stage updated", extra={"turn_id": turn_id, "stage": stage})


async def complete_turn(
    turn_id: str,
    final_response: str,
    *,
    scene_changes: dict | None = None,
    check_results: list | None = None,
) -> None:
    """Mark a pending turn as complete and update the scene atomically (#6).

    A single transaction marks the turn complete, appends to scene turn_history,
    and applies scene_changes to scene_state — all or nothing.

    Idempotent (#10): calling this on an already-complete turn is a no-op.
    """
    async with _session() as db:
        result = await db.execute(select(PendingTurn).where(PendingTurn.id == turn_id))
        pt = result.scalar_one_or_none()
        if pt is None:
            logger.error("Pending turn not found for completion", extra={"turn_id": turn_id})
            return

        # (#10) Idempotency guard — don't double-complete
        if pt.stage == "complete":
            logger.warning(
                "Turn already complete, skipping duplicate completion",
                extra={"turn_id": turn_id},
            )
            return

        # Mark turn complete
        pt.stage = "complete"
        pt.final_response = final_response
        pt.updated_at = datetime.now(UTC)

        # (#8) Persist check_results on the completed turn for consistency
        if check_results is not None:
            pt.check_results = check_results

        # Update scene in the same transaction (atomic commit — Invariant)
        scene_result = await db.execute(select(Scene).where(Scene.id == pt.scene_id))
        scene = scene_result.scalar_one_or_none()
        if scene is not None:
            scene.turn_count += 1

            # Store full response in turn_history (#11 — no truncation)
            history_entry = {
                "turn_id": turn_id,
                "player_input": pt.player_input,
                "npc_response": final_response,
                "turn_type": pt.turn_type,
            }
            if pt.check_results:
                history_entry["checks"] = pt.check_results
            current_history = list(scene.turn_history or [])
            current_history.append(history_entry)
            scene.turn_history = current_history

            # Apply scene_changes to scene_state (#2)
            if scene_changes:
                current_state = dict(scene.scene_state or {})
                _apply_scene_changes(current_state, scene_changes)
                scene.scene_state = current_state

            scene.updated_at = datetime.now(UTC)

        # Single commit — atomic (#6)
        await db.commit()

    logger.info("Turn completed atomically", extra={"turn_id": turn_id})


def _apply_scene_changes(scene_state: dict, scene_changes: dict) -> None:
    """Apply LLM-proposed scene changes to the authoritative scene_state.

    Updates emotional_temperature, appends notes, and manages environment_add/remove.
    All environmental effects are validated against VALID_ENVIRONMENT_EFFECTS.
    """
    # Emotional temperature
    delta = scene_changes.get("emotional_temperature_delta", 0)
    if isinstance(delta, (int, float)):
        current = scene_state.get("emotional_temperature", 0.5)
        scene_state["emotional_temperature"] = max(0.0, min(1.0, current + delta))

    # Notes (append to scene log)
    notes = scene_changes.get("notes", "")
    if notes:
        scene_notes = scene_state.get("scene_notes", [])
        scene_notes.append(notes)
        # Keep last 20 notes to avoid unbounded growth
        scene_state["scene_notes"] = scene_notes[-20:]

    # Environmental effects (#7): add validated effects
    env_add = scene_changes.get("environment_add", [])
    if env_add:
        current_effects = set(scene_state.get("environmental_effects", []))
        for effect in env_add:
            if effect in VALID_ENVIRONMENT_EFFECTS:
                current_effects.add(effect)
            else:
                logger.debug(
                    "Invalid environment effect rejected",
                    extra={"effect": effect, "valid": list(VALID_ENVIRONMENT_EFFECTS)},
                )
        scene_state["environmental_effects"] = sorted(current_effects)

    # Environmental effects (#7): remove effects
    env_remove = scene_changes.get("environment_remove", [])
    if env_remove:
        current_effects = set(scene_state.get("environmental_effects", []))
        for effect in env_remove:
            current_effects.discard(effect)
        scene_state["environmental_effects"] = sorted(current_effects)


async def fail_turn(turn_id: str, error_message: str) -> None:
    """Mark a pending turn as failed."""
    await update_stage(turn_id, "failed", error_message=error_message)


async def mark_stale_turns(
    player_id: str,
    *,
    session_id: str | None = None,
    threshold_seconds: int = _STALE_TURN_THRESHOLD_SECONDS,
) -> int:
    """Mark turns stuck longer than threshold as failed (#1).

    Returns the number of turns marked stale. Called on WebSocket connect
    and session state fetch to clean up orphaned turns from relay crashes.

    Age filtering is done in SQL to avoid loading all incomplete turns into
    memory when only a few are actually stale.
    """
    now = datetime.now(UTC)
    from datetime import timedelta

    cutoff = now - timedelta(seconds=threshold_seconds)

    async with _session() as db:
        query = (
            select(PendingTurn)
            .where(PendingTurn.player_id == player_id)
            .where(PendingTurn.stage.notin_(["complete", "failed"]))
            .where(PendingTurn.created_at < cutoff)
        )
        # (#5) Scope to session if provided
        if session_id:
            query = query.join(Scene).where(Scene.session_id == session_id)

        result = await db.execute(query)
        turns = list(result.scalars().all())

        for pt in turns:
            pt.stage = "failed"
            # Calculate actual age for the error message
            created = pt.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_seconds = int((now - created).total_seconds())
            pt.error_message = f"stale_timeout ({age_seconds}s)"
            pt.updated_at = now

        stale_count = len(turns)
        if stale_count > 0:
            await db.commit()
            logger.info(
                "Stale turns marked failed",
                extra={"player_id": player_id, "stale_count": stale_count},
            )

    return stale_count


async def get_pending_turns(
    player_id: str,
    *,
    scene_id: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Retrieve incomplete pending turns for recovery.

    (#5) When session_id is provided, only returns turns belonging to
    scenes in that session — prevents leaking data from other sessions.
    """
    async with _session() as db:
        query = (
            select(PendingTurn)
            .where(PendingTurn.player_id == player_id)
            .where(PendingTurn.stage.notin_(["complete", "failed"]))
        )
        if scene_id:
            query = query.where(PendingTurn.scene_id == scene_id)
        # (#5) Filter by session if provided
        if session_id:
            query = query.join(Scene).where(Scene.session_id == session_id)

        query = query.order_by(PendingTurn.created_at)

        result = await db.execute(query)
        turns = result.scalars().all()

        return [
            {
                "turn_id": t.id,
                "scene_id": t.scene_id,
                "npc_id": t.npc_id,
                "turn_type": t.turn_type,
                "stage": t.stage,
                "player_input": t.player_input,
                "character_snapshot": t.character_snapshot,
                "check_results": t.check_results,
                "animation_directives": t.animation_directives,
                "scene_changes": t.scene_changes,
                "final_response": t.final_response,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "retry_count": t.retry_count,
                "parent_turn_id": t.parent_turn_id,
            }
            for t in turns
        ]


async def get_scene_turn_history(scene_id: str) -> list[dict[str, str]] | None:
    """Load completed turn history for a scene as LLM message pairs.

    Returns a list of {role, content} dicts suitable for passing to the
    Anthropic messages API, or None if the scene does not exist.
    """
    async with _session() as db:
        result = await db.execute(select(Scene).where(Scene.id == scene_id))
        scene = result.scalar_one_or_none()
        if scene is None:
            return None

        messages: list[dict[str, str]] = []
        for entry in scene.turn_history or []:
            player_input = entry.get("player_input", "")
            npc_response = entry.get("npc_response", "")
            if player_input:
                messages.append({"role": "user", "content": player_input})
            if npc_response:
                messages.append({"role": "assistant", "content": npc_response})
        return messages


async def load_scene_state(scene_id: str) -> dict | None:
    """Load the authoritative scene_state from the DB.

    Returns the scene_state dict, or None if the scene does not exist.
    The relay is the source of truth (Keystone Principle) — clients must
    not supply scene_state in their messages.
    """
    async with _session() as db:
        result = await db.execute(select(Scene).where(Scene.id == scene_id))
        scene = result.scalar_one_or_none()
        if scene is None:
            return None
        return dict(scene.scene_state or {})


async def get_pending_turn(turn_id: str) -> dict | None:
    """Retrieve a single pending turn by ID."""
    async with _session() as db:
        result = await db.execute(select(PendingTurn).where(PendingTurn.id == turn_id))
        t = result.scalar_one_or_none()
        if t is None:
            return None
        return {
            "turn_id": t.id,
            "scene_id": t.scene_id,
            "npc_id": t.npc_id,
            "turn_type": t.turn_type,
            "stage": t.stage,
            "player_input": t.player_input,
            "character_snapshot": t.character_snapshot,
            "analysis_result": t.analysis_result,
            "check_results": t.check_results,
            "animation_directives": t.animation_directives,
            "scene_changes": t.scene_changes,
            "final_response": t.final_response,
            "error_message": t.error_message,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "retry_count": t.retry_count,
            "parent_turn_id": t.parent_turn_id,
        }


async def get_retry_count(scene_id: str, player_input: str) -> int:
    """Get the number of prior failed attempts for a given player input in a scene (#4).

    Used to determine the retry_count for a new pending turn when the client
    retries after a failure. Uses SQL COUNT(*) to avoid loading all rows.
    """
    from sqlalchemy import func

    async with _session() as db:
        result = await db.execute(
            select(func.count())
            .select_from(PendingTurn)
            .where(PendingTurn.scene_id == scene_id)
            .where(PendingTurn.player_input == player_input)
            .where(PendingTurn.stage == "failed")
        )
        return result.scalar() or 0
