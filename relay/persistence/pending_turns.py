"""Pending turn persistence -- write-ahead for crash recovery.

Each function opens and commits its own DB session so that stage
transitions are durable even if the caller crashes afterwards.
Invariant #12: session state persisted before processing.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import select

import relay.database as _db
from relay.models import PendingTurn, Scene

logger = logging.getLogger(__name__)

STAGES = ("received", "analysis", "checks_resolved", "streaming", "complete", "failed")


@asynccontextmanager
async def _session():
    """Open a short-lived DB session from the canonical factory.

    Uses relay.database.AsyncSessionLocal so that test overrides via
    ``import relay.database; relay.database.AsyncSessionLocal = ...``
    propagate automatically — no separate set_session_factory needed.
    """
    async with _db.AsyncSessionLocal() as session:
        yield session


async def create_pending_turn(
    *,
    scene_id: str,
    player_id: str,
    npc_id: str,
    turn_type: str,
    player_input: str,
    character_snapshot: dict | None = None,
) -> str:
    """Write a pending turn record before any processing begins. Returns turn ID."""
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
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(pt)
        await db.commit()
    logger.info("Pending turn created", extra={"turn_id": turn_id, "scene_id": scene_id, "stage": "received"})
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
    """Advance a pending turn to a new stage, persisting intermediate results."""
    async with _session() as db:
        result = await db.execute(select(PendingTurn).where(PendingTurn.id == turn_id))
        pt = result.scalar_one_or_none()
        if pt is None:
            logger.error("Pending turn not found for stage update", extra={"turn_id": turn_id})
            return

        pt.stage = stage
        pt.updated_at = datetime.now(timezone.utc)

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


async def complete_turn(turn_id: str, final_response: str) -> None:
    """Mark a pending turn as complete with the final response."""
    await update_stage(turn_id, "complete", final_response=final_response)

    async with _session() as db:
        result = await db.execute(select(PendingTurn).where(PendingTurn.id == turn_id))
        pt = result.scalar_one_or_none()
        if pt is None:
            return

        scene_result = await db.execute(select(Scene).where(Scene.id == pt.scene_id))
        scene = scene_result.scalar_one_or_none()
        if scene is not None:
            scene.turn_count += 1
            history_entry = {
                "turn_id": turn_id,
                "player_input": pt.player_input,
                "npc_response": final_response[:500],
                "turn_type": pt.turn_type,
            }
            if pt.check_results:
                history_entry["checks"] = pt.check_results
            current_history = list(scene.turn_history or [])
            current_history.append(history_entry)
            scene.turn_history = current_history
            scene.updated_at = datetime.now(timezone.utc)
            await db.commit()


async def fail_turn(turn_id: str, error_message: str) -> None:
    """Mark a pending turn as failed."""
    await update_stage(turn_id, "failed", error_message=error_message)


async def get_pending_turns(player_id: str, scene_id: str | None = None) -> list[dict]:
    """Retrieve incomplete pending turns for recovery."""
    async with _session() as db:
        query = (
            select(PendingTurn)
            .where(PendingTurn.player_id == player_id)
            .where(PendingTurn.stage.notin_(["complete", "failed"]))
        )
        if scene_id:
            query = query.where(PendingTurn.scene_id == scene_id)
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
                "check_results": t.check_results,
                "animation_directives": t.animation_directives,
                "scene_changes": t.scene_changes,
                "final_response": t.final_response,
                "created_at": t.created_at.isoformat() if t.created_at else None,
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
        }
