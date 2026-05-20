"""POST /checks/implicit — standalone check resolution endpoint.

Used for out-of-dialogue checks: trap disarm during traversal, gathering
pre-checks, DM-initiated skill challenges, or any context where the
WebSocket dialogue handler isn't active.

Step 10 improvement #2.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.checks.resolver import (
    is_incapable_of_checks,
    resolve_check,
    resolve_contested_check,
    validate_check,
    validate_checks_batch,
)
from relay.database import get_db
from relay.endpoints._helpers import load_character_any, load_character_owned

logger = logging.getLogger(__name__)

router = APIRouter(tags=["checks"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class ImplicitCheckRequest(BaseModel):
    """Request body for resolving one or more implicit skill checks."""

    character_id: str
    checks: list[dict] = Field(..., min_length=1, max_length=5)


class CheckResultItem(BaseModel):
    skill: str
    dc: int
    reason: str = ""
    roll: int
    dice: list[int]
    roll_mode: str
    modifier: int
    total: int
    passed: bool
    natural_20: bool = False
    natural_1: bool = False
    auto_fail_reason: str | None = None


class ImplicitCheckResponse(BaseModel):
    results: list[CheckResultItem]
    checks_resolved: int


class ContestedCheckRequest(BaseModel):
    """Request body for resolving a contested check between two characters."""

    attacker_character_id: str
    defender_character_id: str
    attacker_skill: str
    attacker_dc: int = 10  # Not used for contested, but validates skill
    defender_skill: str
    defender_dc: int = 10
    reason: str = ""


class ContestedCheckResponse(BaseModel):
    attacker: CheckResultItem
    defender: CheckResultItem
    winner: str
    attacker_total: int
    defender_total: int
    tie: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/checks/implicit", response_model=ImplicitCheckResponse)
async def post_implicit_checks(
    body: ImplicitCheckRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ImplicitCheckResponse:
    """Resolve one or more implicit skill checks for a character.

    Validates and clamps LLM-proposed checks, then resolves against the
    character's ability scores, proficiencies, and conditions.
    """
    char = await load_character_owned(db, body.character_id, token.player_id)

    conditions = char.conditions or []

    # (#5) Check if character can make checks at all
    if is_incapable_of_checks(conditions):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incapacitated",
                "message": "Character cannot make skill checks while stunned or incapacitated",
            },
        )

    # (#11) Validate and cap checks
    validated = validate_checks_batch(body.checks)

    results = []
    for vc in validated:
        check_result = resolve_check(
            vc,
            char.ability_scores,
            char.skill_proficiencies or [],
            char.level,
            conditions=conditions,
        )
        results.append(CheckResultItem(**check_result))

    return ImplicitCheckResponse(
        results=results,
        checks_resolved=len(results),
    )


@router.post("/checks/contested", response_model=ContestedCheckResponse)
async def post_contested_check(
    body: ContestedCheckRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ContestedCheckResponse:
    """Resolve a contested check between two characters (#6).

    Used for grapple (athletics vs athletics/acrobatics), deception vs insight,
    stealth vs perception, etc. Ties go to the defender.
    """
    attacker = await load_character_any(db, body.attacker_character_id)
    defender = await load_character_any(db, body.defender_character_id)

    attacker_check = validate_check({"skill": body.attacker_skill, "dc": 0, "reason": body.reason})
    defender_check = validate_check({"skill": body.defender_skill, "dc": 0, "reason": body.reason})

    contest_result = resolve_contested_check(
        attacker_check,
        attacker.ability_scores,
        attacker.skill_proficiencies or [],
        attacker.level,
        defender_check,
        defender.ability_scores,
        defender.skill_proficiencies or [],
        defender.level,
        attacker_conditions=attacker.conditions or [],
        defender_conditions=defender.conditions or [],
    )

    return ContestedCheckResponse(
        attacker=CheckResultItem(**contest_result["attacker"]),
        defender=CheckResultItem(**contest_result["defender"]),
        winner=contest_result["winner"],
        attacker_total=contest_result["attacker_total"],
        defender_total=contest_result["defender_total"],
        tie=contest_result["tie"],
    )
