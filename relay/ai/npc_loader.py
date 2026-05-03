from __future__ import annotations

import json
import logging
from pathlib import Path

from relay.schemas import NpcPersonality

logger = logging.getLogger(__name__)

_NPCS_ROOT = Path(__file__).parents[2] / "npcs"

_cache: dict[str, NpcPersonality] = {}


def load_npc(npc_id: str) -> NpcPersonality | None:
    """Load and cache an NPC personality file from disk.

    Searches every world sub-directory under npcs/ for a file named {npc_id}.json.
    """
    if npc_id in _cache:
        return _cache[npc_id]

    for path in _NPCS_ROOT.rglob(f"{npc_id}.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            npc = NpcPersonality.model_validate(data)
            _cache[npc_id] = npc
            logger.info("NPC loaded", extra={"npc_id": npc_id, "path": str(path)})
            return npc
        except Exception:
            logger.exception("Failed to load NPC", extra={"npc_id": npc_id, "path": str(path)})
            return None

    logger.warning("NPC not found", extra={"npc_id": npc_id})
    return None


def reload_npc(npc_id: str) -> NpcPersonality | None:
    """Force re-read from disk (hot-reload for content editing)."""
    _cache.pop(npc_id, None)
    return load_npc(npc_id)


def clear_cache() -> None:
    _cache.clear()
