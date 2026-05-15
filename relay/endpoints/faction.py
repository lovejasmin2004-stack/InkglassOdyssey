"""Faction reputation endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.database import get_db
from relay.economy.pricing import faction_tier
from relay.factions.reputation import apply_standing_change
from relay.models import Character
from relay.world.content_loader import load_faction_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/factions", tags=["factions"])


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
    result = await db.execute(
        select(Character).where(
            Character.id == body.character_id,
            Character.player_id == token.player_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    standings = dict(char.faction_standing or {})
    faction_registry = load_faction_registry(token.world_id)
    changes = apply_standing_change(
        standings,
        faction_id,
        body.delta,
        faction_registry=faction_registry or None,
    )
    char.faction_standing = standings
    char.updated_at = datetime.now(UTC)
    flag_modified(char, "faction_standing")

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
    result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.player_id == token.player_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    standings = char.faction_standing or {}
    return AllStandingsResponse(
        standings=[
            FactionStandingResponse(
                faction_id=fid,
                standing=val,
                tier=faction_tier(val),
            )
            for fid, val in standings.items()
        ],
    )
