"""Validates sample fixture files against JSON schemas and Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from relay.schemas import Ability, CharacterSheet, Item, NpcPersonality, WorldConfig

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


class TestAbility:
    def test_json_schema_valid(self) -> None:
        schema = _load_schema("ability")
        data = _load_fixture("sample_ability")
        jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_model_valid(self) -> None:
        data = _load_fixture("sample_ability")
        ability = Ability.model_validate(data)
        assert ability.level_requirement >= 1
        assert ability.level_requirement <= 20
        assert ability.cost.amount >= 0

    def test_json_schema_rejects_invalid_level_requirement(self) -> None:
        schema = _load_schema("ability")
        data = _load_fixture("sample_ability")
        data["level_requirement"] = 25
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_missing_cost(self) -> None:
        schema = _load_schema("ability")
        data = _load_fixture("sample_ability")
        del data["cost"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_rejects_extra_fields(self) -> None:
        data = _load_fixture("sample_ability")
        data["unknown_field"] = "bad"
        with pytest.raises(ValidationError):
            Ability.model_validate(data)


class TestItem:
    def test_json_schema_valid(self) -> None:
        schema = _load_schema("item")
        data = _load_fixture("sample_item")
        jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_model_valid(self) -> None:
        data = _load_fixture("sample_item")
        item = Item.model_validate(data)
        assert item.value >= 0
        assert item.weight >= 0
        assert item.type in {"weapon", "armour", "shield", "consumable", "material", "tool", "quest"}

    def test_json_schema_rejects_invalid_type(self) -> None:
        schema = _load_schema("item")
        data = _load_fixture("sample_item")
        data["type"] = "vehicle"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_negative_value(self) -> None:
        schema = _load_schema("item")
        data = _load_fixture("sample_item")
        data["value"] = -10
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_rejects_invalid_rarity(self) -> None:
        data = _load_fixture("sample_item")
        data["rarity"] = "mythic"
        with pytest.raises(ValidationError):
            Item.model_validate(data)


class TestWorldConfig:
    def test_json_schema_valid(self) -> None:
        schema = _load_schema("world_config")
        data = _load_fixture("sample_world_config")
        jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_model_valid(self) -> None:
        data = _load_fixture("sample_world_config")
        config = WorldConfig.model_validate(data)
        assert config.content_rating in {"moderate", "mature"}
        assert len(config.specialisation_paths) >= 1
        assert config.rest_rules.long_rest_hp_percent == 1.0
        assert config.economy_config.sell_back_ratio >= 0

    def test_json_schema_rejects_invalid_content_rating(self) -> None:
        schema = _load_schema("world_config")
        data = _load_fixture("sample_world_config")
        data["content_rating"] = "explicit"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_missing_specialisation_paths(self) -> None:
        schema = _load_schema("world_config")
        data = _load_fixture("sample_world_config")
        del data["specialisation_paths"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_json_schema_rejects_empty_specialisation_paths(self) -> None:
        schema = _load_schema("world_config")
        data = _load_fixture("sample_world_config")
        data["specialisation_paths"] = []
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=data, schema=schema)

    def test_pydantic_validates_rest_rules(self) -> None:
        data = _load_fixture("sample_world_config")
        config = WorldConfig.model_validate(data)
        assert config.rest_rules.short_rest_hp_percent == 0.25
        assert config.rest_rules.long_rest_exhaustion_reduction == 1

    def test_pydantic_validates_economy_config(self) -> None:
        data = _load_fixture("sample_world_config")
        config = WorldConfig.model_validate(data)
        assert config.economy_config.sell_back_ratio == 0.4
        assert config.economy_config.crafting_margin_target == 0.3

    def test_pydantic_rejects_sell_back_ratio_above_1(self) -> None:
        data = _load_fixture("sample_world_config")
        data["economy_config"]["sell_back_ratio"] = 1.5
        with pytest.raises(ValidationError):
            WorldConfig.model_validate(data)
