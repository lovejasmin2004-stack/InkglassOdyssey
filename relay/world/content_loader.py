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

    Cache consistency: takes a snapshot of cached entries under the lock,
    reads uncached files outside the lock (no I/O under lock), then merges
    newly loaded entries back into the cache atomically.  This prevents
    inconsistent snapshots where some factions reflect the old state and
    others reflect a concurrent invalidation.
    """
    world_dir = _FACTIONS_ROOT / world_id
    if not world_dir.is_dir():
        return {}

    prefix = f"{world_id}:"

    # Snapshot cached entries under the lock (fast — no I/O)
    async with _faction_lock:
        cached_snapshot = {k: v for k, v in _faction_cache.items() if k.startswith(prefix)}

    registry: dict[str, dict[str, Any]] = {}
    uncached: dict[str, dict[str, Any]] = {}

    for path in sorted(world_dir.glob("*.json")):
        faction_id = path.stem
        cache_key = f"{world_id}:{faction_id}"

        if cache_key in cached_snapshot:
            registry[faction_id] = cached_snapshot[cache_key]
            continue

        try:
            data = await asyncio.to_thread(_read_json, path)
            _validate_faction(data)
            registry[faction_id] = data
            uncached[cache_key] = data
        except (json.JSONDecodeError, OSError, ValidationError) as exc:
            logger.warning(
                "Skipping malformed faction file",
                extra={"path": str(path), "error": str(exc)},
            )

    # Merge newly loaded entries into cache atomically
    if uncached:
        async with _faction_lock:
            for cache_key, data in uncached.items():
                _faction_cache[cache_key] = data
            while len(_faction_cache) > _MAX_CACHE_SIZE:
                _faction_cache.popitem(last=False)
            # Touch previously-cached entries used in this registry
            for cache_key in cached_snapshot:
                if cache_key in _faction_cache:
                    _faction_cache.move_to_end(cache_key)

    # Warn about asymmetric relationships (content authoring issues)
    _warn_asymmetric_relationships(registry)

    return registry


def _warn_asymmetric_relationships(registry: dict[str, dict[str, Any]]) -> None:
    """Log warnings for one-directional ally/rival relationships.

    A lists B as ally but B doesn't list A → asymmetric.  Not an error
    (propagation still works), but usually indicates a content authoring
    mistake.
    """
    for fid, fdef in registry.items():
        for ally_id in fdef.get("allied_factions", []):
            if ally_id in registry:
                ally_def = registry[ally_id]
                if fid not in ally_def.get("allied_factions", []):
                    logger.warning(
                        "Asymmetric ally relationship: %s lists %s as ally but %s does not reciprocate",
                        fid,
                        ally_id,
                        ally_id,
                        extra={"faction_id": fid, "ally_id": ally_id},
                    )

        for rival_id in fdef.get("rival_factions", []):
            if rival_id in registry:
                rival_def = registry[rival_id]
                if fid not in rival_def.get("rival_factions", []):
                    logger.warning(
                        "Asymmetric rival relationship: %s lists %s as rival but %s does not reciprocate",
                        fid,
                        rival_id,
                        rival_id,
                        extra={"faction_id": fid, "rival_id": rival_id},
                    )
