"""POST /dice/roll — generic dice rolling endpoint.

Used for DM-initiated rolls, manual player rolls, or any context needing
server-authoritative randomness outside of the check/combat resolvers.

Step 10 improvement #3.
"""

from __future__ import annotations

import logging
import random
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dice"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class DiceRollRequest(BaseModel):
    """Request body for rolling dice.

    notation: standard dice notation like "2d6", "1d20", "3d8+5".
    count: number of times to repeat the full roll (default 1).
    """

    notation: str = Field(..., pattern=r"^\d*d\d+([+-]\d+)?$")
    count: int = Field(default=1, ge=1, le=20)
    reason: str = ""


class SingleRoll(BaseModel):
    dice: list[int]
    modifier: int = 0
    total: int


class DiceRollResponse(BaseModel):
    notation: str
    rolls: list[SingleRoll]
    grand_total: int
    reason: str = ""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def _parse_notation(notation: str) -> tuple[int, int, int]:
    """Parse dice notation like '2d6+3' into (count, sides, modifier)."""
    notation = notation.strip().lower()

    # Split modifier
    modifier = 0
    if "+" in notation:
        parts = notation.split("+")
        notation = parts[0]
        modifier = int(parts[1])
    elif "-" in notation:
        # Only the last '-' is the modifier separator
        idx = notation.rfind("-")
        if idx > 0:
            modifier = -int(notation[idx + 1 :])
            notation = notation[:idx]

    # Split dice
    if "d" not in notation:
        raise ValueError(f"Invalid notation: {notation}")

    d_parts = notation.split("d")
    count = int(d_parts[0]) if d_parts[0] else 1
    sides = int(d_parts[1])

    if count < 1 or count > 100:
        raise ValueError(f"Dice count out of range: {count}")
    if sides < 2 or sides > 100:
        raise ValueError(f"Dice sides out of range: {sides}")

    return count, sides, modifier


@router.post("/dice/roll", response_model=DiceRollResponse)
async def post_dice_roll(
    body: DiceRollRequest,
    _token: Annotated[SessionTokenPayload, Depends(require_session_token)],
) -> DiceRollResponse:
    """Roll dice using standard notation. Server-authoritative randomness.

    Examples: "2d6", "1d20+5", "4d8-2". Use count > 1 for repeated rolls
    (e.g., rolling 6 ability scores as "4d6" with count=6).
    """
    try:
        dice_count, sides, modifier = _parse_notation(body.notation)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_notation", "message": str(e)},
        ) from None

    rolls: list[SingleRoll] = []
    grand_total = 0

    for _ in range(body.count):
        dice = [random.randint(1, sides) for _ in range(dice_count)]
        total = sum(dice) + modifier
        rolls.append(SingleRoll(dice=dice, modifier=modifier, total=total))
        grand_total += total

    logger.info(
        "Dice rolled",
        extra={
            "notation": body.notation,
            "count": body.count,
            "grand_total": grand_total,
            "reason": body.reason,
        },
    )

    return DiceRollResponse(
        notation=body.notation,
        rolls=rolls,
        grand_total=grand_total,
        reason=body.reason,
    )
