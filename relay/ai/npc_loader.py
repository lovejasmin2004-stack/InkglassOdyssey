from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from relay.schemas import NpcPersonality

logger = logging.getLogger(__name__)

_NPCS_ROOT = Path(__file__).parents[2] / "npcs"

_cache: dict[str, NpcPersonality] = {}


class NpcLoadError(Exception):
    """NPC file exists but could not be parsed or validated."""


def _cache_key(world_id: str, npc_id: str) -> str:
    return f"{world_id}:{npc_id}"


def load_npc(npc_id: str, world_id: str | None = None) -> NpcPersonality | None:
    """Load and cache an NPC personality file from disk.

    When *world_id* is given, looks only under ``npcs/{world_id}/``.
    Otherwise searches every world sub-directory.

    Raises NpcLoadError if the file exists but contains invalid JSON or
    fails schema validation — callers must distinguish this from a
    genuinely missing file (returns None).
    """
    if world_id:
        key = _cache_key(world_id, npc_id)
        if key in _cache:
            return _cache[key]
        return _load_from_path(
            _NPCS_ROOT / world_id / f"{npc_id}.json",
            npc_id,
            world_id,
        )

    for key, npc in _cache.items():
        if key == npc_id or key.endswith(f":{npc_id}"):
            return npc

    for path in _NPCS_ROOT.rglob(f"{npc_id}.json"):
        resolved_world = path.parent.name
        return _load_from_path(path, npc_id, resolved_world)

    logger.warning("NPC not found", extra={"npc_id": npc_id})
    return None


def _load_from_path(
    path: Path, npc_id: str, world_id: str
) -> NpcPersonality | None:
    if not path.exists():
        logger.warning("NPC not found", extra={"npc_id": npc_id, "path": str(path)})
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise NpcLoadError(
            f"NPC file {path} exists but could not be read: {exc}"
        ) from exc

    try:
        npc = NpcPersonality.model_validate(data)
    except ValidationError as exc:
        raise NpcLoadError(
            f"NPC file {path} failed schema validation: {exc}"
        ) from exc

    key = _cache_key(world_id, npc_id)
    _cache[key] = npc
    logger.info("NPC loaded", extra={"npc_id": npc_id, "world_id": world_id, "path": str(path)})
    return npc


def reload_npc(npc_id: str, world_id: str | None = None) -> NpcPersonality | None:
    """Force re-read from disk (hot-reload for content editing)."""
    if world_id:
        _cache.pop(_cache_key(world_id, npc_id), None)
    else:
        keys_to_remove = [k for k in _cache if k == npc_id or k.endswith(f":{npc_id}")]
        for k in keys_to_remove:
            _cache.pop(k, None)
    return load_npc(npc_id, world_id)


def clear_cache() -> None:
    _cache.clear()
