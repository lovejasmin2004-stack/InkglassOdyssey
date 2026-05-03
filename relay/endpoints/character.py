from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.models import Character

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/character", tags=["character"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CharacterCreate(BaseModel):
    world_id: str
    name: str = Field(min_length=1)
    specialisation_path_id: str
    ability_scores: dict[str, int]
    skill_proficiencies: list[str] = Field(default_factory=list)
    saving_throw_proficiencies: list[str] = Field(default_factory=list, min_length=0)

    model_config = {"extra": "forbid"}


class CharacterPatch(BaseModel):
    name: str | None = None
    level: int | None = Field(default=None, ge=1, le=20)
    specialisation_path_id: str | None = None
    ability_scores: dict[str, int] | None = None
    skill_proficiencies: list[str] | None = None
    saving_throw_proficiencies: list[str] | None = None
    hp_current: int | None = None
    hp_max: int | None = Field(default=None, ge=1)
    ac: int | None = Field(default=None, ge=0)
    passive_checks: dict[str, int] | None = None
    conditions: list[dict] | None = None
    exhaustion_level: int | None = Field(default=None, ge=0, le=6)
    resources: dict | None = None
    wallet: dict[str, int] | None = None
    inventory: list[dict] | None = None
    equipped_gear: dict[str, str] | None = None
    known_recipes: list[str] | None = None
    companions: list[dict] | None = None
    rp_voice_notes: str | None = None
    relationships: dict[str, int] | None = None
    faction_standing: dict[str, int] | None = None

    model_config = {"extra": "forbid"}


class CharacterResponse(BaseModel):
    id: str
    player_id: str
    world_id: str
    name: str
    level: int
    specialisation_path_id: str
    ability_scores: dict[str, int]
    skill_proficiencies: list[str]
    saving_throw_proficiencies: list[str]
    hp_current: int
    hp_max: int
    ac: int
    passive_checks: dict[str, int]
    conditions: list[dict]
    exhaustion_level: int
    resources: dict
    wallet: dict[str, int]
    inventory: list[dict]
    equipped_gear: dict[str, str]
    known_recipes: list[str]
    companions: list[dict]
    rp_voice_notes: str | None
    relationships: dict[str, int]
    faction_standing: dict[str, int]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ability_modifier(score: int) -> int:
    return (score - 10) // 2


def _compute_defaults(data: CharacterCreate) -> dict:
    """Compute hp_max, ac, and passive_checks from ability scores."""
    scores = data.ability_scores
    con_mod = _ability_modifier(scores.get("constitution", scores.get("con", 10)))
    dex_mod = _ability_modifier(scores.get("dexterity", scores.get("dex", 10)))
    wis_mod = _ability_modifier(scores.get("wisdom", scores.get("wis", 10)))

    # d8 hit die default; will be overridden by specialisation path in later phase
    hp_max = max(1, 8 + con_mod)

    # Unarmoured AC: 10 + DEX
    ac = 10 + dex_mod

    # Passive perception as baseline passive check
    passive_checks = {"perception": 10 + wis_mod}

    return {"hp_max": hp_max, "ac": ac, "passive_checks": passive_checks}


def _row_to_response(row: Character) -> CharacterResponse:
    return CharacterResponse.model_validate(row)


def _assert_owns(token: AccountTokenPayload | SessionTokenPayload, character: Character) -> None:
    if character.player_id != token.player_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"code": "forbidden", "message": "Character belongs to another player"})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED, response_model=CharacterResponse)
async def create_character(body: CharacterCreate, token: Token, db: DB) -> CharacterResponse:
    defaults = _compute_defaults(body)
    now = datetime.now(timezone.utc)

    # Default saving throw proficiencies to first two ability score keys if not supplied
    saving_throws = body.saving_throw_proficiencies
    if len(saving_throws) < 2:
        saving_throws = list(body.ability_scores.keys())[:2]

    character = Character(
        id=str(uuid.uuid4()),
        player_id=token.player_id,
        world_id=body.world_id,
        name=body.name,
        level=1,
        specialisation_path_id=body.specialisation_path_id,
        ability_scores=body.ability_scores,
        skill_proficiencies=body.skill_proficiencies,
        saving_throw_proficiencies=saving_throws,
        hp_current=defaults["hp_max"],
        hp_max=defaults["hp_max"],
        ac=defaults["ac"],
        passive_checks=defaults["passive_checks"],
        conditions=[],
        exhaustion_level=0,
        resources={},
        wallet={body.world_id: 0},
        inventory=[],
        equipped_gear={},
        known_recipes=[],
        companions=[],
        rp_voice_notes=None,
        relationships={},
        faction_standing={},
        created_at=now,
        updated_at=now,
    )
    db.add(character)
    await db.flush()
    await db.refresh(character)

    logger.info("Character created", extra={"character_id": character.id, "player_id": token.player_id})
    return _row_to_response(character)


@router.get("/{character_id}", response_model=CharacterResponse)
async def get_character(character_id: str, token: Token, db: DB) -> CharacterResponse:
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()

    if character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "Character not found"})
    _assert_owns(token, character)
    return _row_to_response(character)


@router.patch("/{character_id}", response_model=CharacterResponse)
async def patch_character(character_id: str, body: CharacterPatch, token: Token, db: DB) -> CharacterResponse:
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()

    if character is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"code": "not_found", "message": "Character not found"})
    _assert_owns(token, character)

    updates = body.model_dump(exclude_none=True)

    # Validate merged state before touching the ORM object so malformed
    # values never reach the database.
    preview = {c.key: getattr(character, c.key) for c in Character.__table__.columns}
    preview.update(updates)
    try:
        CharacterResponse.model_validate(preview)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        )

    for field, value in updates.items():
        setattr(character, field, value)
    character.updated_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(character)

    logger.info("Character patched", extra={"character_id": character_id, "fields": list(updates.keys())})
    return _row_to_response(character)
