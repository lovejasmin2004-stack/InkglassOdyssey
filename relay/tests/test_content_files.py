"""Validate every authored content file against its JSON schema.

CI step 1 (CLAUDE.md §7.5): no content file may bypass schema validation.
Uses the Admin Workshop's content-type registry so the two never drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from relay.admin.reload import CONTENT_TYPES, WORLD_IDS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _REPO_ROOT / "schemas"

# Files that hold an array of records rather than one record, each checked
# against its own schema instead of the directory's default.
_ARRAY_FILES: dict[str, str] = {
    "scenarios/*/blueprints.json": "event_arc_blueprint.json",
    "templates/*/npc_templates.json": "npc_template.json",
}


def _validator(schema_file: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((_SCHEMAS / schema_file).read_text(encoding="utf-8")))


def _is_array_file(path: Path) -> bool:
    return any(path.match(pattern) for pattern in _ARRAY_FILES)


def _single_record_files() -> list[tuple[Path, str]]:
    cases = []
    for cfg in CONTENT_TYPES.values():
        if cfg["schema"] is None:
            continue
        for path in sorted((_REPO_ROOT / cfg["dir"]).glob("*/*.json")):
            if path.stem in cfg.get("exclude", set()) or _is_array_file(path):
                continue
            cases.append((path, cfg["schema"]))
    for path in sorted(_REPO_ROOT.glob("regions/*/world_config.json")):
        cases.append((path, "world_config.json"))
    return cases


def _array_files() -> list[tuple[Path, str]]:
    return [(path, schema) for pattern, schema in _ARRAY_FILES.items() for path in sorted(_REPO_ROOT.glob(pattern))]


def _rel(case: tuple[Path, str]) -> str:
    return str(case[0].relative_to(_REPO_ROOT))


@pytest.mark.parametrize("schema_path", sorted(_SCHEMAS.glob("*.json")), ids=lambda p: p.name)
def test_schema_is_well_formed(schema_path: Path) -> None:
    jsonschema.Draft202012Validator.check_schema(json.loads(schema_path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("case", _single_record_files(), ids=_rel)
def test_content_file_matches_schema(case: tuple[Path, str]) -> None:
    path, schema_file = case
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = [f"{list(e.absolute_path)}: {e.message}" for e in _validator(schema_file).iter_errors(data)]
    assert not errors, f"{_rel(case)} fails {schema_file}:\n" + "\n".join(errors)


@pytest.mark.parametrize("case", _single_record_files(), ids=_rel)
def test_content_filename_matches_id(case: tuple[Path, str]) -> None:
    """CLAUDE.md §7.3: filenames match the ``id`` field."""
    path, _ = case
    if path.name == "world_config.json":
        pytest.skip("world_config.json is keyed by world_id")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("id") == path.stem


@pytest.mark.parametrize("case", _array_files(), ids=_rel)
def test_array_content_file_matches_schema(case: tuple[Path, str]) -> None:
    path, schema_file = case
    records = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(records, list), f"{_rel(case)} must be a JSON array"
    validator = _validator(schema_file)
    errors = [
        f"[{i}] {record.get('id', '?')} {list(e.absolute_path)}: {e.message}"
        for i, record in enumerate(records)
        for e in validator.iter_errors(record)
    ]
    assert not errors, f"{_rel(case)} fails {schema_file}:\n" + "\n".join(errors)


def test_content_world_directories_are_known_worlds() -> None:
    """Every {content}/{world_id}/ directory names a registered world."""
    dirs = {cfg["dir"] for cfg in CONTENT_TYPES.values()} | {"templates"}
    unknown = sorted(
        str(world_dir.relative_to(_REPO_ROOT))
        for d in dirs
        for world_dir in (_REPO_ROOT / d).glob("*/")
        if world_dir.is_dir() and world_dir.name not in WORLD_IDS
    )
    assert not unknown, f"Unknown world directories: {unknown}"
