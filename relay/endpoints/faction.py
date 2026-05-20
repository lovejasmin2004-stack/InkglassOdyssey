"""Faction reputation endpoints."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.database import get_db
from relay.factions.reputation import apply_standing_change, resolve_tier
from relay.models import Character, FactionStandingLog
from relay.world.content_loader import load_faction_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/factions", tags=["factions"])

# ---------------------------------------------------------------------------
# Per-character rate limiting for faction standing changes
# ---------------------------------------------------------------------------

_FACTION_CHANGE_WINDOW = 60.0  # seconds
_FACTION_CHANGE_MAX = 10  # max changes per character per window

# {character_id: [timestamp, ...]} — recent change timestamps
_faction_change_log: dict[str, list[float]] = {}


def _check_faction_rate_limit(character_id: str) -> None:
    """Enforce per-character rate limit on faction standing changes.

    Raises HTTP 429 if the character has exceeded the maximum number of
    standing changes within the rolling window.
    """
    now = time.monotonic()
    timestamps = _faction_change_log.get(character_id, [])
    # Prune expired entries
    timestamps = [t for t in timestamps if now - t < _FACTION_CHANGE_WINDOW]
    if len(timestamps) >= _FACTION_CHANGE_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "faction_rate_limited",
                "message": "Too many faction standing changes. Please slow down.",
            },
        )
    timestamps.append(now)
    _faction_change_log[character_id] = timestamps


def clear_faction_rate_limits() -> None:
    """Reset faction rate limit state. Used by tests."""
    _faction_change_log.clear()


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class StandingChangeRequest(BaseModel):
    character_id: str
    delta: int = Field(ge=-100, le=100)
    reason: str = ""

    model_config = {"extra": "forbid"}


class FactionChange(BaseModel):
    faction_id: str
    old: int
    new: int
    tier: str
    old_tier: str
    tier_changed: bool
    source: str


class StandingChangeResponse(BaseModel):
    faction_id: str
    changes: list[FactionChange]


class FactionStandingResponse(BaseModel):
    faction_id: str
    standing: int
    tier: str


class AllStandingsResponse(BaseModel):
    standings: list[FactionStandingResponse]


class StandingLogEntry(BaseModel):
    id: str
    faction_id: str
    old_standing: int
    new_standing: int
    delta: int
    old_tier: str
    new_tier: str
    tier_changed: bool
    source: str
    source_faction_id: str | None
    reason: str
    session_id: str | None
    created_at: datetime


class StandingLogResponse(BaseModel):
    entries: list[StandingLogEntry]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.patch("/{faction_id}/standing", response_model=StandingChangeResponse)
async def patch_standing(
    faction_id: str,
    body: StandingChangeRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StandingChangeResponse:
    """Change a character's faction standing with automatic propagation.

    Propagation: allied factions get +50% of the delta, rival factions get -25%.
    """
    _check_faction_rate_limit(body.character_id)

    query_result = await db.execute(
        select(Character).where(
            Character.id == body.character_id,
            Character.player_id == token.player_id,
        )
    )
    char = query_result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    standings = dict(char.faction_standing or {})
    faction_registry = await load_faction_registry(token.world_id)
    changes = apply_standing_change(
        standings,
        faction_id,
        body.delta,
        faction_registry=faction_registry or None,
        reason=body.reason,
    )
    char.faction_standing = standings
    char.updated_at = datetime.now(UTC)
    flag_modified(char, "faction_standing")

    # Write audit log entries for every changed faction
    _write_standing_log(
        db,
        changes=changes,
        player_id=token.player_id,
        character_id=body.character_id,
        world_id=token.world_id,
        source_faction_id=faction_id,
        session_id=token.session_id,
    )

    await db.commit()

    logger.info(
        "Faction standing updated",
        extra={
            "character_id": char.id,
            "faction_id": faction_id,
            "delta": body.delta,
            "reason": body.reason,
            "changes": changes,
        },
    )

    return StandingChangeResponse(
        faction_id=faction_id,
        changes=[FactionChange(faction_id=fid, **data) for fid, data in changes.items()],
    )


@router.get("/{character_id}/standings", response_model=AllStandingsResponse)
async def get_standings(
    character_id: str,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AllStandingsResponse:
    """Get all faction standings for a character."""
    query_result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.player_id == token.player_id,
        )
    )
    char = query_result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    standings = char.faction_standing or {}
    faction_registry = await load_faction_registry(token.world_id)

    standings_list: list[FactionStandingResponse] = []
    for fid, val in standings.items():
        # Use per-faction custom thresholds when available
        faction_def = (faction_registry or {}).get(fid)
        thresholds = faction_def.get("reputation_thresholds") if faction_def else None
        tier = resolve_tier(val, thresholds)
        standings_list.append(FactionStandingResponse(faction_id=fid, standing=val, tier=tier))

    return AllStandingsResponse(standings=standings_list)


@router.get("/{character_id}/log", response_model=StandingLogResponse)
async def get_standing_log(
    character_id: str,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
    faction_id: str | None = Query(default=None, description="Filter by faction"),
    limit: int = Query(default=50, ge=1, le=200, description="Max entries"),
    offset: int = Query(default=0, ge=0, description="Entries to skip"),
) -> StandingLogResponse:
    """Read the faction standing change log for a character.

    Optionally filtered by faction_id. Ordered by created_at descending
    (most recent first).
    """
    # Verify character ownership
    char_result = await db.execute(
        select(Character.id).where(
            Character.id == character_id,
            Character.player_id == token.player_id,
        )
    )
    if char_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    # Build query
    query = (
        select(FactionStandingLog)
        .where(FactionStandingLog.character_id == character_id)
        .order_by(FactionStandingLog.created_at.desc())
    )
    if faction_id is not None:
        query = query.where(FactionStandingLog.faction_id == faction_id)

    # Count total before pagination
    from sqlalchemy import func

    count_query = select(func.count()).select_from(query.with_only_columns(FactionStandingLog.id).subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # Apply pagination
    query = query.offset(offset).limit(limit)
    result = await db.execute(query)
    rows = result.scalars().all()

    return StandingLogResponse(
        entries=[
            StandingLogEntry(
                id=row.id,
                faction_id=row.faction_id,
                old_standing=row.old_standing,
                new_standing=row.new_standing,
                delta=row.delta,
                old_tier=row.old_tier,
                new_tier=row.new_tier,
                tier_changed=row.tier_changed,
                source=row.source,
                source_faction_id=row.source_faction_id,
                reason=row.reason,
                session_id=row.session_id,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
    )


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------


def _write_standing_log(
    db: AsyncSession,
    *,
    changes: dict[str, dict],
    player_id: str,
    character_id: str,
    world_id: str,
    source_faction_id: str,
    session_id: str | None = None,
) -> None:
    """Write FactionStandingLog entries for all changes in a single operation."""
    for fid, data in changes.items():
        log_entry = FactionStandingLog(
            id=str(uuid.uuid4()),
            player_id=player_id,
            character_id=character_id,
            world_id=world_id,
            faction_id=fid,
            old_standing=data["old"],
            new_standing=data["new"],
            delta=data["new"] - data["old"],
            old_tier=data["old_tier"],
            new_tier=data["tier"],
            tier_changed=data["tier_changed"],
            source=data["source"],
            source_faction_id=source_faction_id if data["source"] != "direct" else None,
            reason=data.get("reason", ""),
            session_id=session_id,
        )
        db.add(log_entry)
