"""Companion endpoints — recruit, combat action, incapacitate, dismiss, status."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.companions.combat_ai import resolve_companion_action
from relay.companions.loyalty import (
    apply_dismissal,
    handle_incapacitation,
    recover_after_combat,
)
from relay.companions.manager import (
    AffectionTooLowError,
    AlreadyRecruitedError,
    CompanionLimitError,
    ConditionNotMetError,
    add_companion,
    create_companion_entry,
    find_companion,
    remove_companion,
    validate_recruitment,
)
from relay.database import get_db
from relay.models import Character

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companions", tags=["companions"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class RecruitRequest(BaseModel):
    character_id: str
    npc_id: str
    companion_data: dict
    npc_hp_max: int = Field(ge=1)
    max_active_companions: int = Field(default=1, ge=1)
    world_flags: dict[str, bool] | None = None

    model_config = {"extra": "forbid"}


class RecruitResponse(BaseModel):
    npc_id: str
    recruited: bool
    companion_state: dict


class CombatActionRequest(BaseModel):
    character_id: str
    companion_data: dict
    target: dict | None = None
    allies: list[dict] | None = None

    model_config = {"extra": "forbid"}


class CombatActionResponse(BaseModel):
    action: dict


class IncapacitateRequest(BaseModel):
    character_id: str
    companion_data: dict

    model_config = {"extra": "forbid"}


class IncapacitateResponse(BaseModel):
    result: dict


class DismissRequest(BaseModel):
    character_id: str
    companion_data: dict

    model_config = {"extra": "forbid"}


class DismissResponse(BaseModel):
    result: dict


class CompanionStatus(BaseModel):
    npc_id: str
    hp_current: int
    hp_max: int
    exhaustion_level: int
    loyalty_strain: int
    behavior_type: str
    active: bool


class CompanionsResponse(BaseModel):
    companions: list[CompanionStatus]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_character(db: AsyncSession, character_id: str, player_id: str) -> Character:
    result = await db.execute(
        select(Character).where(
            Character.id == character_id,
            Character.player_id == player_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "character_not_found",
                "message": "Character not found",
            },
        )
    return char


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/recruit", response_model=RecruitResponse)
async def post_recruit(
    body: RecruitRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecruitResponse:
    """Recruit a companion NPC."""
    char = await _load_character(db, body.character_id, token.player_id)

    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})
    relationship_score = relationships.get(body.npc_id, 0)

    try:
        validate_recruitment(
            npc_id=body.npc_id,
            companion_data=body.companion_data,
            relationship_score=relationship_score,
            current_companions=companions,
            max_active_companions=body.max_active_companions,
            world_flags=body.world_flags,
        )
    except AlreadyRecruitedError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_recruited",
                "message": f"{body.npc_id} is already a companion",
            },
        ) from None
    except CompanionLimitError:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "companion_limit",
                "message": "Companion limit reached",
            },
        ) from None
    except AffectionTooLowError:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "affection_too_low",
                "message": "Relationship score too low",
            },
        ) from None
    except ConditionNotMetError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "condition_not_met",
                "message": str(exc),
            },
        ) from None

    entry = create_companion_entry(
        npc_id=body.npc_id,
        companion_data=body.companion_data,
        npc_hp_max=body.npc_hp_max,
    )
    companions = add_companion(companions, entry)
    char.companions = companions
    flag_modified(char, "companions")

    await db.commit()

    logger.info(
        "Companion recruited via endpoint",
        extra={"character_id": char.id, "npc_id": body.npc_id},
    )

    return RecruitResponse(
        npc_id=body.npc_id,
        recruited=True,
        companion_state=entry,
    )


@router.post("/{companion_id}/combat-action", response_model=CombatActionResponse)
async def post_combat_action(
    companion_id: str,
    body: CombatActionRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CombatActionResponse:
    """Resolve one automatic combat action for a companion."""
    char = await _load_character(db, body.character_id, token.player_id)
    companions = list(char.companions or [])

    comp = find_companion(companions, companion_id)
    if not comp:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "companion_not_found",
                "message": f"Companion {companion_id} not found",
            },
        )

    action = resolve_companion_action(
        companion=comp,
        companion_data=body.companion_data,
        target=body.target,
        allies=body.allies,
    )

    return CombatActionResponse(action=action)


@router.post("/{companion_id}/incapacitate", response_model=IncapacitateResponse)
async def post_incapacitate(
    companion_id: str,
    body: IncapacitateRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IncapacitateResponse:
    """Process companion incapacitation (0 HP in combat)."""
    char = await _load_character(db, body.character_id, token.player_id)
    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})

    comp = find_companion(companions, companion_id)
    if not comp:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "companion_not_found",
                "message": f"Companion {companion_id} not found",
            },
        )

    result = handle_incapacitation(
        companion=comp,
        companion_data=body.companion_data,
        relationships=relationships,
    )

    char.companions = companions
    char.relationships = relationships
    flag_modified(char, "companions")
    flag_modified(char, "relationships")

    await db.commit()

    return IncapacitateResponse(result=result)


@router.post("/{companion_id}/recover")
async def post_recover(
    companion_id: str,
    body: CombatActionRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Recover a companion after combat ends."""
    char = await _load_character(db, body.character_id, token.player_id)
    companions = list(char.companions or [])

    comp = find_companion(companions, companion_id)
    if not comp:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "companion_not_found",
                "message": f"Companion {companion_id} not found",
            },
        )

    recover_after_combat(comp)

    char.companions = companions
    flag_modified(char, "companions")
    await db.commit()

    return {"npc_id": companion_id, "recovered": True, "companion_state": comp}


@router.post("/{companion_id}/dismiss", response_model=DismissResponse)
async def post_dismiss(
    companion_id: str,
    body: DismissRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DismissResponse:
    """Dismiss a companion (triggers farewell_template)."""
    char = await _load_character(db, body.character_id, token.player_id)
    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})

    comp = find_companion(companions, companion_id)
    if not comp:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "companion_not_found",
                "message": f"Companion {companion_id} not found",
            },
        )

    result = apply_dismissal(
        companion=comp,
        companion_data=body.companion_data,
        relationships=relationships,
    )

    companions = remove_companion(companions, companion_id)
    char.companions = companions
    char.relationships = relationships
    flag_modified(char, "companions")
    flag_modified(char, "relationships")

    await db.commit()

    return DismissResponse(result=result)


@router.get("/{character_id}", response_model=CompanionsResponse)
async def get_companions(
    character_id: str,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompanionsResponse:
    """Get all companions for a character."""
    char = await _load_character(db, character_id, token.player_id)
    companions = char.companions or []

    return CompanionsResponse(
        companions=[
            CompanionStatus(
                npc_id=c["npc_id"],
                hp_current=c.get("hp_current", 0),
                hp_max=c.get("hp_max", 0),
                exhaustion_level=c.get("exhaustion_level", 0),
                loyalty_strain=c.get("loyalty_strain", 0),
                behavior_type=c.get("behavior_type", "defensive"),
                active=c.get("active", True),
            )
            for c in companions
        ]
    )
