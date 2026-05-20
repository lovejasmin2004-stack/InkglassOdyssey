"""Content file operations for the Library Workshop.

Reads, writes, lists, and validates content JSON files against their
schemas.  All writes are atomic (Invariant #16): write to a temp file,
validate, then rename into place.  On any failure the original file is
untouched.

Schema mapping is data-driven — no hardcoded forms (Invariant #24).

Deletes are soft: the original file is moved to a ``.trash/`` directory
with a timestamp (Invariant #19: archive before clearing).

All public functions are async and offload blocking I/O to threads to
avoid starving the event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS_ROOT = _REPO_ROOT / "schemas"

# Strict ID pattern — lower-case snake_case starting with a letter.
SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

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

WORLD_IDS: frozenset[str] = frozenset(
    {
        "inkglass_dark",
        "murim",
        "cybernightlife",
        "wha_au",
        "atla_au",
        "gachiakuta_au",
        "hxh_au",
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _content_dir(content_type: str, world_id: str) -> Path:
    """Return the content directory, verifying it stays inside the repo root."""
    cfg = CONTENT_TYPES[content_type]
    directory = (_REPO_ROOT / cfg["dir"] / world_id).resolve()
    if not directory.is_relative_to(_REPO_ROOT):
        raise ValueError(f"Path traversal blocked: {directory}")
    return directory


def _content_path(content_type: str, world_id: str, file_id: str) -> Path:
    """Return the full path to a content file, with traversal check."""
    directory = _content_dir(content_type, world_id)
    path = (directory / f"{file_id}.json").resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f"Path traversal blocked: {path}")
    return path


def _schema_path(content_type: str) -> Path | None:
    schema_file = CONTENT_TYPES[content_type]["schema"]
    if schema_file is None:
        return None
    return _SCHEMAS_ROOT / schema_file


# ---------------------------------------------------------------------------
# Schema caching (process-lifetime — schemas change only on code deploy)
# ---------------------------------------------------------------------------

_schema_cache: dict[str, dict] = {}
_validator_cache: dict[str, jsonschema.Draft202012Validator] = {}


def _load_schema_sync(content_type: str) -> dict | None:
    """Load and cache a JSON schema (blocking I/O, call from thread)."""
    sp = _schema_path(content_type)
    if sp is None:
        return None
    cache_key = str(sp)
    if cache_key in _schema_cache:
        return _schema_cache[cache_key]
    if not sp.exists():
        return None
    schema = json.loads(sp.read_text(encoding="utf-8"))
    _schema_cache[cache_key] = schema
    return schema


def _get_validator_sync(content_type: str) -> jsonschema.Draft202012Validator | None:
    """Get a cached validator instance for a content type."""
    sp = _schema_path(content_type)
    if sp is None:
        return None
    cache_key = str(sp)
    if cache_key in _validator_cache:
        return _validator_cache[cache_key]
    schema = _load_schema_sync(content_type)
    if schema is None:
        return None
    validator = jsonschema.Draft202012Validator(schema)
    _validator_cache[cache_key] = validator
    return validator


# ---------------------------------------------------------------------------
# Listing index cache (invalidated on write/delete)
# ---------------------------------------------------------------------------

_index_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _build_index_sync(content_type: str, world_id: str) -> list[dict[str, Any]]:
    """Scan directory and build lightweight id+name index (blocking I/O)."""
    directory = _content_dir(content_type, world_id)
    if not directory.is_dir():
        return []

    results: list[dict[str, Any]] = []
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


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def list_schemas() -> list[dict[str, str]]:
    """Return metadata for every JSON schema in /schemas/."""

    def _scan() -> list[dict[str, str]]:
        results = []
        for path in sorted(_SCHEMAS_ROOT.glob("*.json")):
            results.append({"name": path.stem, "filename": path.name})
        return results

    return await asyncio.to_thread(_scan)


async def read_schema(name: str) -> dict | None:
    """Read a schema by stem name (e.g. 'npc_personality')."""

    def _read() -> dict | None:
        path = _SCHEMAS_ROOT / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    return await asyncio.to_thread(_read)


def list_worlds() -> list[str]:
    """Return configured world IDs."""
    return sorted(WORLD_IDS)


async def list_content(content_type: str, world_id: str) -> list[dict[str, Any]]:
    """List all content files of a given type for a world.

    Uses a cached index that is invalidated on write/delete.
    """
    cache_key = (content_type, world_id)
    if cache_key in _index_cache:
        return _index_cache[cache_key]

    result = await asyncio.to_thread(_build_index_sync, content_type, world_id)
    _index_cache[cache_key] = result
    return result


async def read_content(content_type: str, world_id: str, file_id: str) -> dict[str, Any] | None:
    """Read a single content file. Returns ``{data, etag}`` or None."""

    def _read() -> dict[str, Any] | None:
        path = _content_path(content_type, world_id, file_id)
        if not path.exists():
            return None
        raw = path.read_bytes()
        etag = hashlib.sha256(raw).hexdigest()[:16]
        data = json.loads(raw.decode("utf-8"))
        return {"data": data, "etag": etag}

    return await asyncio.to_thread(_read)


async def validate_content(content_type: str, data: dict) -> list[str]:
    """Validate data against the content type's schema.

    Returns a list of error messages (empty = valid).
    Uses cached schema and validator instances.
    """

    def _validate() -> list[str]:
        validator = _get_validator_sync(content_type)
        if validator is None:
            sp = _schema_path(content_type)
            if sp is not None and not sp.exists():
                return [f"Schema file not found: {sp.name}"]
            return []
        return [e.message for e in validator.iter_errors(data)]

    return await asyncio.to_thread(_validate)


async def write_content(
    content_type: str,
    world_id: str,
    file_id: str,
    data: dict,
    *,
    expected_etag: str | None = None,
) -> list[str]:
    """Validate and atomically write a content file.

    Returns validation errors (empty list on success).
    Invariant #16: complete or rollback, no partial state.

    If *expected_etag* is provided and does not match the current file's
    content hash, returns a conflict error.
    """
    errors = await validate_content(content_type, data)
    if errors:
        return errors

    def _write() -> list[str]:
        directory = _content_dir(content_type, world_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = _content_path(content_type, world_id, file_id)

        # Optimistic concurrency check
        if expected_etag is not None and target.exists():
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
            if current_hash != expected_etag:
                return [f"Conflict: file was modified since last read (expected {expected_etag}, got {current_hash})"]

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
        return []

    result = await asyncio.to_thread(_write)
    if not result:
        logger.info(
            "Content file written",
            extra={"content_type": content_type, "world_id": world_id, "file_id": file_id},
        )
        await invalidate_cache(content_type, world_id, file_id)
    return result


async def delete_content(content_type: str, world_id: str, file_id: str) -> bool:
    """Soft-delete a content file by moving it to ``.trash/``.

    Returns True if deleted, False if not found.
    Invariant #19: archive before clearing.
    """

    def _delete() -> bool:
        path = _content_path(content_type, world_id, file_id)
        if not path.exists():
            return False
        trash_dir = _content_dir(content_type, world_id) / ".trash"
        trash_dir.mkdir(exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        trash_path = trash_dir / f"{file_id}_{timestamp}.json"
        path.rename(trash_path)
        return True

    deleted = await asyncio.to_thread(_delete)
    if deleted:
        logger.info(
            "Content file soft-deleted",
            extra={"content_type": content_type, "world_id": world_id, "file_id": file_id},
        )
        await invalidate_cache(content_type, world_id, file_id)
    return deleted


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


async def invalidate_cache(content_type: str, world_id: str, file_id: str) -> None:
    """Invalidate relay caches after a content file write or delete.

    Awaits the invalidation and logs failures rather than silently
    swallowing them.
    """
    # Always invalidate the listing index
    _index_cache.pop((content_type, world_id), None)

    try:
        if content_type == "npcs":
            from relay.ai.npc_loader import reload_npc

            await reload_npc(file_id, world_id)

        elif content_type == "factions":
            from relay.world.content_loader import invalidate_faction

            await invalidate_faction(file_id, world_id)

        elif content_type == "items":
            from relay.world.item_loader import invalidate_item

            await invalidate_item(file_id, world_id)

        elif content_type == "crafting":
            from relay.crafting.recipe_loader import invalidate_recipe

            await invalidate_recipe(file_id, world_id)

        elif content_type == "regions":
            from relay.crafting.region_loader import invalidate_region

            await invalidate_region(file_id, world_id)

        elif content_type == "fauna":
            from relay.crafting.fauna_loader import invalidate_fauna

            await invalidate_fauna(file_id, world_id)

    except Exception:
        logger.exception(
            "Cache invalidation failed",
            extra={
                "content_type": content_type,
                "world_id": world_id,
                "file_id": file_id,
            },
        )
