"""Shared endpoint helpers — character loading, common error patterns."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.companions.manager import find_companion
from relay.models import Character
from relay.schemas import NpcPersonality

logger = logging.getLogger(__name__)


async def load_character_owned(db: AsyncSession, character_id: str, player_id: str) -> Character:
    """Load a character and verify ownership. Returns 404 / 403."""
    result = await db.execute(select(Character).where(Character.id == character_id))
    char = result.scalar_one_or_none()
    if char is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    if char.player_id != player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Character belongs to another player"},
        )
    return char


async def load_character_any(db: AsyncSession, character_id: str) -> Character:
    """Load a character without ownership check (NPC targets, contested checks)."""
    result = await db.execute(select(Character).where(Character.id == character_id))
    char = result.scalar_one_or_none()
    if char is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    return char


# ---------------------------------------------------------------------------
# Companion helpers
# ---------------------------------------------------------------------------


def find_companion_or_404(companions: list[dict], companion_id: str) -> dict:
    """Find a companion entry in the list by npc_id, or raise HTTP 404."""
    comp = find_companion(companions, companion_id)
    if not comp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "companion_not_found",
                "message": f"Companion {companion_id} not found",
            },
        )
    return comp


async def load_companion_npc_or_404(npc_id: str, world_id: str) -> NpcPersonality:
    """Load an NPC personality that has companion_data, or raise 404/400/500."""
    from relay.ai.npc_loader import NpcLoadError, load_npc

    try:
        npc = await load_npc(npc_id, world_id)
    except NpcLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "npc_load_error", "message": str(exc)},
        ) from None
    if npc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"NPC '{npc_id}' not found"},
        )
    if npc.companion_data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "not_a_companion", "message": f"NPC '{npc_id}' is not a companion"},
        )
    return npc


# ---------------------------------------------------------------------------
# World config loading (shared cache)
# ---------------------------------------------------------------------------

_MAX_WORLD_CONFIG_CACHE = 16
_world_config_cache: OrderedDict[str, dict] = OrderedDict()
_world_config_lock = asyncio.Lock()


def _read_world_config(config_path: Path) -> dict:
    """Blocking I/O — run via to_thread."""
    return json.loads(config_path.read_text(encoding="utf-8"))


async def load_world_config(world_id: str) -> dict | None:
    """Load and cache world config from disk."""
    async with _world_config_lock:
        if world_id in _world_config_cache:
            _world_config_cache.move_to_end(world_id)
            return _world_config_cache[world_id]

    config_path = Path(__file__).parents[1] / ".." / "regions" / world_id / "world_config.json"
    config_path = config_path.resolve()
    if not config_path.exists():
        config_path = (Path(__file__).parents[1] / ".." / "worlds" / f"{world_id}.json").resolve()
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


async def get_max_active_companions(world_id: str) -> int:
    """Look up max_active_companions from world config. Falls back to 1."""
    config = await load_world_config(world_id)
    if config:
        return config.get("max_active_companions", 1)
    return 1
