"""Validates sample fixture files against JSON schemas and Pydantic models."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from relay.schemas import CharacterSheet, NpcPersonality

SCHEMAS_DIR = Path(__file__).parents[2] / "schemas"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text())


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


class TestCharacterSheet:
    def test_json_schema_valid(self) -> None:
        schema = _load_schema("character_sheet")
        data = _load_fixture("sample_character")
        jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_model_valid(self) -> None:
        data = _load_fixture("sample_character")
        sheet = CharacterSheet.model_validate(data)
        assert sheet.level >= 1
        assert sheet.level <= 20
        assert len(sheet.saving_throw_proficiencies) == 2

    def test_json_schema_rejects_invalid_level(self) -> None:
        schema = _load_schema("character_sheet")
        data = _load_fixture("sample_character")
        data["level"] = 99
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_missing_required_field(self) -> None:
        schema = _load_schema("character_sheet")
        data = _load_fixture("sample_character")
        del data["player_id"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)


class TestNpcPersonality:
    def test_json_schema_valid(self) -> None:
        schema = _load_schema("npc_personality")
        data = _load_fixture("sample_npc")
        jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_model_valid(self) -> None:
        data = _load_fixture("sample_npc")
        npc = NpcPersonality.model_validate(data)
        assert npc.entity_class in {"humanoid", "creature", "spirit", "construct"}
        assert len(npc.few_shot_examples) >= 2
        assert len(npc.secrets) >= 1

    def test_json_schema_rejects_too_few_examples(self) -> None:
        schema = _load_schema("npc_personality")
        data = _load_fixture("sample_npc")
        data["few_shot_examples"] = data["few_shot_examples"][:1]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_invalid_entity_class(self) -> None:
        schema = _load_schema("npc_personality")
        data = _load_fixture("sample_npc")
        data["entity_class"] = "robot"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)
