"""Content file operations for the Library Workshop.

Reads, writes, lists, and validates content JSON files against their
schemas.  All writes are atomic (Invariant #16): write to a temp file,
validate, then rename into place.  On any failure the original file is
untouched.

Schema mapping is data-driven — no hardcoded forms (Invariant #24).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS_ROOT = _REPO_ROOT / "schemas"

CONTENT_TYPES: dict[str, dict[str, Any]] = {
    "npcs": {"dir": "npcs", "schema": "npc_personality.json"},
    "items": {"dir": "items", "schema": "item.json"},
    "abilities": {"dir": "abilities", "schema": "ability.json"},
    "crafting": {"dir": "crafting", "schema": "recipe.json"},
    "quests": {"dir": "quests", "schema": "quest.json"},
    "factions": {"dir": "factions", "schema": "faction.json"},
    "scenarios": {"dir": "scenarios", "schema": "scenario.json"},
    "fauna": {"dir": "fauna", "schema": "fauna.json"},
    "lore": {"dir": "lore", "schema": "lore.json"},
    "regions": {"dir": "regions", "schema": "region.json"},
    "animations": {"dir": "animations", "schema": None},
}

WORLD_IDS = [
    "inkglass_dark",
    "murim",
    "cybernightlife",
    "wha_au",
    "atla_au",
    "gachiakuta_au",
    "hxh_au",
]


def _content_dir(content_type: str, world_id: str) -> Path:
    cfg = CONTENT_TYPES[content_type]
    return _REPO_ROOT / cfg["dir"] / world_id


def _schema_path(content_type: str) -> Path | None:
    schema_file = CONTENT_TYPES[content_type]["schema"]
    if schema_file is None:
        return None
    return _SCHEMAS_ROOT / schema_file


def list_schemas() -> list[dict[str, str]]:
    """Return metadata for every JSON schema in /schemas/."""
    results = []
    for path in sorted(_SCHEMAS_ROOT.glob("*.json")):
        results.append({"name": path.stem, "filename": path.name})
    return results


def read_schema(name: str) -> dict | None:
    """Read a schema by stem name (e.g. 'npc_personality')."""
    path = _SCHEMAS_ROOT / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_worlds() -> list[str]:
    """Return configured world IDs."""
    return list(WORLD_IDS)


def list_content(content_type: str, world_id: str) -> list[dict[str, Any]]:
    """List all content files of a given type for a world.

    Returns a list of {id, name (if present)} dicts.
    """
    directory = _content_dir(content_type, world_id)
    if not directory.is_dir():
        return []

    results = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry: dict[str, Any] = {"id": path.stem}
            if "name" in data:
                entry["name"] = data["name"]
            results.append(entry)
        except (json.JSONDecodeError, OSError):
            results.append({"id": path.stem, "name": f"(error reading {path.name})"})
    return results


def read_content(content_type: str, world_id: str, file_id: str) -> dict[str, Any] | None:
    """Read a single content file. Returns None if not found."""
    path = _content_dir(content_type, world_id) / f"{file_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def validate_content(content_type: str, data: dict) -> list[str]:
    """Validate data against the content type's schema.

    Returns a list of error messages (empty = valid).
    """
    schema_path = _schema_path(content_type)
    if schema_path is None:
        return []
    if not schema_path.exists():
        return [f"Schema file not found: {schema_path.name}"]

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(data)]


def write_content(content_type: str, world_id: str, file_id: str, data: dict) -> list[str]:
    """Validate and atomically write a content file.

    Returns validation errors (empty list on success).
    Invariant #16: complete or rollback, no partial state.
    """
    errors = validate_content(content_type, data)
    if errors:
        return errors

    directory = _content_dir(content_type, world_id)
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / f"{file_id}.json"
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    fd, tmp_path = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    fd_closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd_closed = True
        os.replace(tmp_path, str(target))
    except Exception:
        if not fd_closed:
            os.close(fd)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    logger.info(
        "Content file written",
        extra={"content_type": content_type, "world_id": world_id, "file_id": file_id},
    )
    invalidate_cache(content_type, world_id, file_id)
    return []


def delete_content(content_type: str, world_id: str, file_id: str) -> bool:
    """Delete a content file. Returns True if deleted, False if not found."""
    path = _content_dir(content_type, world_id) / f"{file_id}.json"
    if not path.exists():
        return False
    path.unlink()
    logger.info(
        "Content file deleted",
        extra={"content_type": content_type, "world_id": world_id, "file_id": file_id},
    )
    invalidate_cache(content_type, world_id, file_id)
    return True


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def invalidate_cache(content_type: str, world_id: str, file_id: str) -> None:
    """Invalidate relay caches after a content file write or delete.

    Runs the appropriate async invalidation function in the current event loop
    (if one is running) or synchronously clears the cache otherwise.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if content_type == "npcs":
        from relay.ai.npc_loader import reload_npc

        if loop and loop.is_running():
            loop.create_task(reload_npc(file_id, world_id))
        else:
            asyncio.run(reload_npc(file_id, world_id))

    elif content_type == "factions":
        from relay.world.content_loader import invalidate_faction

        if loop and loop.is_running():
            loop.create_task(invalidate_faction(file_id, world_id))
        else:
            asyncio.run(invalidate_faction(file_id, world_id))

    elif content_type == "items":
        from relay.endpoints.shop import invalidate_item

        if loop and loop.is_running():
            loop.create_task(invalidate_item(file_id, world_id))
        else:
            asyncio.run(invalidate_item(file_id, world_id))

    elif content_type == "crafting":
        from relay.crafting.recipe_loader import invalidate_recipe

        if loop and loop.is_running():
            loop.create_task(invalidate_recipe(file_id, world_id))
        else:
            asyncio.run(invalidate_recipe(file_id, world_id))

    elif content_type == "regions":
        from relay.crafting.region_loader import invalidate_region

        if loop and loop.is_running():
            loop.create_task(invalidate_region(file_id, world_id))
        else:
            asyncio.run(invalidate_region(file_id, world_id))
