"""Companion endpoints — recruit, combat action, incapacitate, dismiss, status.

Invariant #1: NPC companion_data is always loaded server-side from the NPC
personality file via ``load_companion_npc_or_404``. The client never supplies it.

Invariant #13: Rate limiting on all mutating endpoints.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.companions.combat_ai import apply_directive, resolve_companion_action
from relay.companions.loyalty import (
    apply_dismissal,
    clear_exhaustion_on_rest,
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
    remove_companion,
    validate_recruitment,
)
from relay.database import get_db
from relay.endpoints._helpers import (
    find_companion_or_404,
    get_max_active_companions,
    load_character_owned,
    load_companion_npc_or_404,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/companions", tags=["companions"])

# ---------------------------------------------------------------------------
# Per-character rate limiting (Invariant #13)
# ---------------------------------------------------------------------------

_COMPANION_CHANGE_WINDOW = 60.0  # seconds
_COMPANION_CHANGE_MAX = 15  # max mutations per character per window

# {character_id: [timestamp, ...]}
_companion_change_log: dict[str, list[float]] = {}


def _check_companion_rate_limit(character_id: str) -> None:
    """Enforce per-character rate limit on companion mutations.

    Raises HTTP 429 if the character has exceeded the maximum number of
    companion changes within the rolling window.
    """
    now = time.monotonic()
    timestamps = _companion_change_log.get(character_id, [])
    timestamps = [t for t in timestamps if now - t < _COMPANION_CHANGE_WINDOW]
    if len(timestamps) >= _COMPANION_CHANGE_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "companion_rate_limited",
                "message": "Too many companion actions. Please slow down.",
            },
        )
    timestamps.append(now)
    _companion_change_log[character_id] = timestamps


def clear_companion_rate_limits() -> None:
    """Reset companion rate limit state. Used by tests."""
    _companion_change_log.clear()


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class RecruitRequest(BaseModel):
    character_id: str
    npc_id: str
    world_flags: dict[str, bool] | None = None

    model_config = {"extra": "forbid"}


class RecruitResponse(BaseModel):
    npc_id: str
    recruited: bool
    companion_state: dict


class CombatActionRequest(BaseModel):
    character_id: str
    target: dict | None = None
    allies: list[dict] | None = None

    model_config = {"extra": "forbid"}


class CombatActionResponse(BaseModel):
    action: dict


class IncapacitateRequest(BaseModel):
    character_id: str

    model_config = {"extra": "forbid"}


class IncapacitateResponse(BaseModel):
    result: dict


class RecoverRequest(BaseModel):
    character_id: str

    model_config = {"extra": "forbid"}


class RecoverResponse(BaseModel):
    npc_id: str
    recovered: bool
    companion_state: dict


class DismissRequest(BaseModel):
    character_id: str

    model_config = {"extra": "forbid"}


class DismissResponse(BaseModel):
    result: dict


class RestRequest(BaseModel):
    character_id: str

    model_config = {"extra": "forbid"}


class RestResponse(BaseModel):
    npc_id: str
    exhaustion_level: int


class DirectiveRequest(BaseModel):
    character_id: str
    directive: str

    model_config = {"extra": "forbid"}


class DirectiveResponse(BaseModel):
    npc_id: str
    old_behavior: str
    new_behavior: str


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
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/recruit", response_model=RecruitResponse)
async def post_recruit(
    body: RecruitRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecruitResponse:
    """Recruit a companion NPC.

    Loads the NPC's companion_data from disk (Invariant #1). The client
    only supplies the npc_id; all NPC data is relay-authoritative.
    """
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    npc = await load_companion_npc_or_404(body.npc_id, token.world_id)

    companion_data = npc.companion_data.model_dump()  # type: ignore[union-attr]
    max_companions = await get_max_active_companions(token.world_id)

    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})
    relationship_score = relationships.get(body.npc_id, 0)

    try:
        validate_recruitment(
            npc_id=body.npc_id,
            companion_data=companion_data,
            relationship_score=relationship_score,
            current_companions=companions,
            max_active_companions=max_companions,
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
        companion_data=companion_data,
        npc_hp_max=npc.hp_max,
    )
    companions = add_companion(companions, entry)
    char.companions = companions
    flag_modified(char, "companions")
    char.updated_at = datetime.now(UTC)

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
    """Resolve one automatic combat action for a companion.

    Stateless: returns the action result dict. The combat system
    (relay/combat/) is responsible for applying damage/healing to state.
    """
    char = await load_character_owned(db, body.character_id, token.player_id)
    npc = await load_companion_npc_or_404(companion_id, token.world_id)
    companions = list(char.companions or [])

    comp = find_companion_or_404(companions, companion_id)

    companion_data = npc.companion_data.model_dump()  # type: ignore[union-attr]
    action = resolve_companion_action(
        companion=comp,
        companion_data=companion_data,
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
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    npc = await load_companion_npc_or_404(companion_id, token.world_id)
    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})

    comp = find_companion_or_404(companions, companion_id)

    companion_data = npc.companion_data.model_dump()  # type: ignore[union-attr]
    result = handle_incapacitation(
        companion=comp,
        companion_data=companion_data,
        relationships=relationships,
    )

    char.companions = companions
    char.relationships = relationships
    flag_modified(char, "companions")
    flag_modified(char, "relationships")
    char.updated_at = datetime.now(UTC)

    await db.commit()

    return IncapacitateResponse(result=result)


@router.post("/{companion_id}/recover", response_model=RecoverResponse)
async def post_recover(
    companion_id: str,
    body: RecoverRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RecoverResponse:
    """Recover a companion after combat ends."""
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    companions = list(char.companions or [])

    comp = find_companion_or_404(companions, companion_id)

    recover_after_combat(comp)

    char.companions = companions
    flag_modified(char, "companions")
    char.updated_at = datetime.now(UTC)
    await db.commit()

    return RecoverResponse(npc_id=companion_id, recovered=True, companion_state=comp)


@router.post("/{companion_id}/dismiss", response_model=DismissResponse)
async def post_dismiss(
    companion_id: str,
    body: DismissRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DismissResponse:
    """Dismiss a companion (triggers farewell_template)."""
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    npc = await load_companion_npc_or_404(companion_id, token.world_id)
    companions = list(char.companions or [])
    relationships = dict(char.relationships or {})

    comp = find_companion_or_404(companions, companion_id)

    companion_data = npc.companion_data.model_dump()  # type: ignore[union-attr]
    result = apply_dismissal(
        companion=comp,
        companion_data=companion_data,
        relationships=relationships,
    )

    companions, _removed = remove_companion(companions, companion_id)
    char.companions = companions
    char.relationships = relationships
    flag_modified(char, "companions")
    flag_modified(char, "relationships")
    char.updated_at = datetime.now(UTC)

    await db.commit()

    return DismissResponse(result=result)


@router.post("/{companion_id}/rest", response_model=RestResponse)
async def post_rest(
    companion_id: str,
    body: RestRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestResponse:
    """Clear one exhaustion level from a companion on rest."""
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    companions = list(char.companions or [])

    comp = find_companion_or_404(companions, companion_id)

    clear_exhaustion_on_rest(comp)

    char.companions = companions
    flag_modified(char, "companions")
    char.updated_at = datetime.now(UTC)
    await db.commit()

    return RestResponse(npc_id=companion_id, exhaustion_level=comp["exhaustion_level"])


@router.post("/{companion_id}/directive", response_model=DirectiveResponse)
async def post_directive(
    companion_id: str,
    body: DirectiveRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DirectiveResponse:
    """Change a companion's behavior type via a prose directive."""
    _check_companion_rate_limit(body.character_id)
    char = await load_character_owned(db, body.character_id, token.player_id)
    npc = await load_companion_npc_or_404(companion_id, token.world_id)
    companions = list(char.companions or [])

    comp = find_companion_or_404(companions, companion_id)

    companion_data = npc.companion_data.model_dump()  # type: ignore[union-attr]
    vocab = companion_data.get("combat_profile", {}).get("directive_vocabulary")
    old_behavior = comp.get("behavior_type", "defensive")

    apply_directive(comp, body.directive, vocab)

    char.companions = companions
    flag_modified(char, "companions")
    char.updated_at = datetime.now(UTC)
    await db.commit()

    return DirectiveResponse(
        npc_id=companion_id,
        old_behavior=old_behavior,
        new_behavior=comp["behavior_type"],
    )


@router.get("/{character_id}", response_model=CompanionsResponse)
async def get_companions(
    character_id: str,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompanionsResponse:
    """Get all companions for a character."""
    char = await load_character_owned(db, character_id, token.player_id)
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
