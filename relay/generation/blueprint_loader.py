"""Load and cache event arc blueprint definitions.

Blueprints live at ``scenarios/{world_id}/blueprints.json`` — an array of
EventArcBlueprint objects.  The loader reads them once and caches by
(world_id, blueprint_id) for fast lookup.

Design doc: docs/design_proposals.md §4 (Blueprint Pattern for Event Arcs)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from relay.schemas import EventArcBlueprint

logger = logging.getLogger(__name__)

_SCENARIOS_ROOT = Path(__file__).parents[2] / "scenarios"

# ---------------------------------------------------------------------------
# Cache: world_id → {blueprint_id → EventArcBlueprint}
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, EventArcBlueprint]] = {}
_cache_lock = asyncio.Lock()


def _read_blueprints(path: Path) -> list[dict]:
    """Blocking I/O — run via to_thread."""
    return json.loads(path.read_text(encoding="utf-8"))


async def load_blueprints(world_id: str) -> dict[str, EventArcBlueprint]:
    """Load all event arc blueprints for a world, keyed by blueprint ID.

    Returns an empty dict if no blueprint file exists.
    """
    async with _cache_lock:
        if world_id in _cache:
            return _cache[world_id]

    path = _SCENARIOS_ROOT / world_id / "blueprints.json"
    if not path.exists():
        logger.warning(
            "No event arc blueprints found",
            extra={"world_id": world_id, "path": str(path)},
        )
        return {}

    try:
        raw_list = await asyncio.to_thread(_read_blueprints, path)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Failed to read event arc blueprints",
            extra={"world_id": world_id, "error": str(exc)},
        )
        return {}

    blueprints: dict[str, EventArcBlueprint] = {}
    for raw in raw_list:
        try:
            bp = EventArcBlueprint.model_validate(raw)
            blueprints[bp.id] = bp
        except Exception:
            logger.exception(
                "Invalid event arc blueprint, skipping",
                extra={"world_id": world_id, "blueprint_id": raw.get("id", "?")},
            )

    async with _cache_lock:
        # Re-check: another coroutine may have populated the cache while
        # we were doing I/O outside the lock.
        if world_id in _cache:
            return _cache[world_id]
        _cache[world_id] = blueprints

    logger.info(
        "Event arc blueprints loaded",
        extra={"world_id": world_id, "count": len(blueprints)},
    )
    return blueprints


async def get_blueprint(world_id: str, blueprint_id: str) -> EventArcBlueprint | None:
    """Get a specific event arc blueprint by world and blueprint ID."""
    blueprints = await load_blueprints(world_id)
    return blueprints.get(blueprint_id)


def clear_cache() -> None:
    """Clear the blueprint cache (for testing / hot-reload)."""
    _cache.clear()
