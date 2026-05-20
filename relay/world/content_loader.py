"""Server-side content file loading.

Invariant #1: The relay is the source of truth. Content files on disk (NPCs,
factions, items, recipes) are the canonical definitions — never trust the
client to supply them.

All loaders follow the same pattern:
  1. Check an in-memory cache keyed by "{world_id}:{content_id}".
  2. On miss, read from {content_root}/{world_id}/{content_id}.json.
  3. Validate via Pydantic model.
  4. Return parsed object or None.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from relay.schemas import Faction

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parents[2]

_FACTIONS_ROOT = _REPO_ROOT / "factions"

_MAX_CACHE_SIZE = 128
_faction_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_faction_lock = asyncio.Lock()


def _read_json(path: Path) -> dict[str, Any]:
    """Blocking I/O — run via to_thread."""
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_faction(data: dict[str, Any]) -> dict[str, Any]:
    """Validate against Pydantic model; returns data unchanged if valid."""
    Faction.model_validate(data)
    return data


async def invalidate_faction(faction_id: str, world_id: str) -> None:
    """Remove a faction from cache so the next load reads fresh from disk."""
    cache_key = f"{world_id}:{faction_id}"
    async with _faction_lock:
        _faction_cache.pop(cache_key, None)


async def invalidate_faction_registry(world_id: str) -> None:
    """Remove all cached factions for a world."""
    prefix = f"{world_id}:"
    async with _faction_lock:
        keys = [k for k in _faction_cache if k.startswith(prefix)]
        for k in keys:
            del _faction_cache[k]


async def load_faction(faction_id: str, world_id: str) -> dict[str, Any] | None:
    """Load a single faction definition from disk."""
    cache_key = f"{world_id}:{faction_id}"
    async with _faction_lock:
        if cache_key in _faction_cache:
            _faction_cache.move_to_end(cache_key)
            return _faction_cache[cache_key]

    path = _FACTIONS_ROOT / world_id / f"{faction_id}.json"
    if not path.exists():
        return None

    try:
        data = await asyncio.to_thread(_read_json, path)
        _validate_faction(data)
        async with _faction_lock:
            _faction_cache[cache_key] = data
            while len(_faction_cache) > _MAX_CACHE_SIZE:
                _faction_cache.popitem(last=False)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load faction file", extra={"path": str(path), "error": str(exc)})
        return None
    except ValidationError as exc:
        logger.warning("Faction file failed validation", extra={"path": str(path), "error": str(exc)})
        return None


async def load_faction_registry(world_id: str) -> dict[str, dict[str, Any]]:
    """Load all faction definitions for a world.

    Returns a dict keyed by faction_id.
    """
    world_dir = _FACTIONS_ROOT / world_id
    if not world_dir.is_dir():
        return {}

    registry: dict[str, dict[str, Any]] = {}
    for path in world_dir.glob("*.json"):
        faction_id = path.stem
        cache_key = f"{world_id}:{faction_id}"
        async with _faction_lock:
            if cache_key in _faction_cache:
                _faction_cache.move_to_end(cache_key)
                registry[faction_id] = _faction_cache[cache_key]
                continue
        try:
            data = await asyncio.to_thread(_read_json, path)
            _validate_faction(data)
            async with _faction_lock:
                _faction_cache[cache_key] = data
                while len(_faction_cache) > _MAX_CACHE_SIZE:
                    _faction_cache.popitem(last=False)
            registry[faction_id] = data
        except (json.JSONDecodeError, OSError, ValidationError) as exc:
            logger.warning(
                "Skipping malformed faction file",
                extra={"path": str(path), "error": str(exc)},
            )
    return registry
