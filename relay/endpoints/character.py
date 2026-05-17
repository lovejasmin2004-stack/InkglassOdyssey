from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.models import Character

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/character", tags=["character"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]

# Tier 2 worlds require tier >= 2 to create characters in.
_TIER2_WORLDS: frozenset[str] = frozenset({"wha_au", "atla_au", "gachiakuta_au", "hxh_au"})

# Fields that only relay subsystems (or DMs in admin mode) may modify.
# Direct client PATCH on these would bypass transaction logging, faction
# propagation, companion validation, or combat resolution.
_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {
        "wallet",
        "inventory",
        "faction_standing",
        "companions",
        "hp_current",
        "hp_max",
        "ac",
        "conditions",
        "exhaustion_level",
        "death_state_exhaustion_gained",
        "resources",
        "equipped_gear",
        "passive_checks",
        "level",
        "relationships",
    }
)

# JSON-typed columns on the Character model that need flag_modified on write.
_JSON_COLUMNS: frozenset[str] = frozenset(
    {
        "ability_scores",
        "skill_proficiencies",
        "saving_throw_proficiencies",
        "passive_checks",
        "conditions",
        "resources",
        "wallet",
        "inventory",
        "equipped_gear",
        "known_recipes",
        "companions",
        "relationships",
        "faction_standing",
    }
)


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
    """Full patch model — includes protected fields for internal/DM use."""

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
    death_state_exhaustion_gained: int | None = Field(default=None, ge=0, le=3)
    resources: dict | None = None
    wallet: dict[str, int] | None = None
    inventory: list[dict] | None = None
    equipped_gear: dict | None = None
    known_recipes: list[str] | None = None
    companions: list[dict] | None = None
    rp_voice_notes: str | None = None
    relationships: dict[str, int] | None = None
    faction_standing: dict[str, int] | None = None

    model_config = {"extra": "forbid"}


class CharacterListItem(BaseModel):
    """Lightweight summary for list endpoints — avoids serializing full nested state."""

    id: str
    player_id: str
    world_id: str
    name: str
    level: int
    specialisation_path_id: str
    hp_current: int
    hp_max: int

    model_config = {"from_attributes": True}


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
    death_state_exhaustion_gained: int
    resources: dict
    wallet: dict[str, int]
    inventory: list[dict]
    equipped_gear: dict
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


_MAX_WORLD_CONFIG_CACHE = 16
_world_config_cache: OrderedDict[str, dict] = OrderedDict()
_world_config_lock = asyncio.Lock()


def _read_world_config(config_path: Path) -> dict:
    """Blocking I/O — run via to_thread."""
    return json.loads(config_path.read_text(encoding="utf-8"))


async def _load_world_config(world_id: str) -> dict | None:
    """Load and cache world config from disk."""
    async with _world_config_lock:
        if world_id in _world_config_cache:
            _world_config_cache.move_to_end(world_id)
            return _world_config_cache[world_id]

    config_path = Path(__file__).parents[2] / "regions" / world_id / "world_config.json"
    if not config_path.exists():
        config_path = Path(__file__).parents[2] / "worlds" / f"{world_id}.json"
    if not config_path.exists():
        logger.warning(
            "World config not found",
            extra={"world_id": world_id, "searched": ["regions/", "worlds/"]},
        )
        return None

    try:
        config = await asyncio.to_thread(_read_world_config, config_path)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Failed to load world config",
            extra={"world_id": world_id, "path": str(config_path), "error": str(exc)},
        )
        return None

    async with _world_config_lock:
        _world_config_cache[world_id] = config
        while len(_world_config_cache) > _MAX_WORLD_CONFIG_CACHE:
            _world_config_cache.popitem(last=False)
    return config


async def _get_hit_die(world_id: str, specialisation_path_id: str) -> int:
    """Look up hit die from world config. Falls back to d8 if not found."""
    config = await _load_world_config(world_id)
    if config:
        for sp in config.get("specialisation_paths", []):
            if sp.get("id") == specialisation_path_id:
                return sp.get("hit_die", 8)
    return 8


async def _compute_defaults(data: CharacterCreate) -> dict:
    """Compute hp_max, ac, and passive_checks from ability scores."""
    scores = data.ability_scores
    con_mod = _ability_modifier(scores.get("constitution", scores.get("con", 10)))
    dex_mod = _ability_modifier(scores.get("dexterity", scores.get("dex", 10)))
    wis_mod = _ability_modifier(scores.get("wisdom", scores.get("wis", 10)))

    hit_die = await _get_hit_die(data.world_id, data.specialisation_path_id)
    hp_max = max(1, hit_die + con_mod)

    # Unarmoured AC: 10 + DEX
    ac = 10 + dex_mod

    # Passive perception as baseline passive check
    passive_checks = {"perception": 10 + wis_mod}

    return {"hp_max": hp_max, "ac": ac, "passive_checks": passive_checks}


def _row_to_response(row: Character) -> CharacterResponse:
    return CharacterResponse.model_validate(row)


def _assert_owns(token: AccountTokenPayload | SessionTokenPayload, character: Character) -> None:
    if character.player_id != token.player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "Character belongs to another player",
            },
        )


def _check_world_tier(world_id: str, token: AccountTokenPayload | SessionTokenPayload) -> None:
    """Reject character creation in Tier 2 worlds if the player lacks access."""
    if world_id in _TIER2_WORLDS and token.tier < 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": f"World '{world_id}' requires Tier 2 access",
            },
        )


def _is_internal_caller(token: AccountTokenPayload | SessionTokenPayload) -> bool:
    """Check if the caller is a DM or if admin mode allows protected writes."""
    from relay.config import settings

    if settings.admin_mode:
        return True
    return isinstance(token, SessionTokenPayload) and token.role == "dm"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CharacterListItem])
async def list_characters(
    token: Token,
    db: DB,
    world_id: str | None = Query(default=None, description="Filter by world"),
) -> list[CharacterListItem]:
    """List all characters owned by the authenticated player."""
    query = select(Character).where(Character.player_id == token.player_id)
    if world_id is not None:
        query = query.where(Character.world_id == world_id)
    result = await db.execute(query)
    rows = result.scalars().all()
    return [CharacterListItem.model_validate(row) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CharacterResponse)
async def create_character(body: CharacterCreate, token: Token, db: DB) -> CharacterResponse:
    _check_world_tier(body.world_id, token)

    defaults = await _compute_defaults(body)
    now = datetime.now(UTC)

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
        death_state_exhaustion_gained=0,
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

    logger.info(
        "Character created",
        extra={"character_id": character.id, "player_id": token.player_id},
    )
    return _row_to_response(character)


@router.get("/{character_id}", response_model=CharacterResponse)
async def get_character(character_id: str, token: Token, db: DB) -> CharacterResponse:
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()

    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    _assert_owns(token, character)
    return _row_to_response(character)


@router.patch("/{character_id}", response_model=CharacterResponse)
async def patch_character(character_id: str, body: CharacterPatch, token: Token, db: DB) -> CharacterResponse:
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()

    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    _assert_owns(token, character)

    updates = body.model_dump(exclude_none=True)

    # Guard protected fields from direct client writes.
    if not _is_internal_caller(token):
        protected_attempted = set(updates.keys()) & _PROTECTED_FIELDS
        if protected_attempted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "forbidden",
                    "message": (
                        f"Cannot directly modify protected fields: "
                        f"{', '.join(sorted(protected_attempted))}. "
                        f"Use the appropriate game endpoint instead."
                    ),
                },
            )

    # Validate merged state before touching the ORM object so malformed
    # values never reach the database.
    preview = {c.key: getattr(character, c.key) for c in Character.__table__.columns}
    preview.update(updates)
    try:
        CharacterResponse.model_validate(preview)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": str(exc)},
        ) from None

    for field, value in updates.items():
        setattr(character, field, value)
        if field in _JSON_COLUMNS:
            flag_modified(character, field)
    character.updated_at = datetime.now(UTC)

    await db.flush()
    await db.refresh(character)

    logger.info(
        "Character patched",
        extra={"character_id": character_id, "fields": list(updates.keys())},
    )
    return _row_to_response(character)
