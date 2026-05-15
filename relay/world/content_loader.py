"""Server-side content file loading.

Invariant #1: The relay is the source of truth. Content files on disk (NPCs,
factions, items, recipes) are the canonical definitions — never trust the
client to supply them.

All loaders follow the same pattern:
  1. Check an in-memory cache keyed by "{world_id}:{content_id}".
  2. On miss, read from {content_root}/{world_id}/{content_id}.json.
  3. Return parsed dict or None.

Phase 0: no cache invalidation. Restart to pick up file changes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parents[2]

_FACTIONS_ROOT = _REPO_ROOT / "factions"

_faction_cache: dict[str, dict[str, Any]] = {}


def load_faction(faction_id: str, world_id: str) -> dict[str, Any] | None:
    """Load a single faction definition from disk."""
    cache_key = f"{world_id}:{faction_id}"
    if cache_key in _faction_cache:
        return _faction_cache[cache_key]

    path = _FACTIONS_ROOT / world_id / f"{faction_id}.json"
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _faction_cache[cache_key] = data
        return data
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to load faction file", extra={"path": str(path)})
        return None


def load_faction_registry(world_id: str) -> dict[str, dict[str, Any]]:
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
        if cache_key in _faction_cache:
            registry[faction_id] = _faction_cache[cache_key]
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _faction_cache[cache_key] = data
            registry[faction_id] = data
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Skipping malformed faction file", extra={"path": str(path)}
            )
    return registry
