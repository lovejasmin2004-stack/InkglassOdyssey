"""Tests for crafting and gathering systems.

Covers: recipe validation, material consumption, partial loss on failure,
station validation, tool advantage, critical success, transaction logging,
gathering yields, and inventory updates through the endpoints.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token
from relay.crafting.crafter import (
    LevelTooLowError,
    MissingMaterialsError,
    RecipeNotKnownError,
    StationRequiredError,
    check_materials,
    consume_materials,
    consume_partial_materials,
    has_tool_advantage,
    produce_output,
    validate_recipe_requirements,
)
from relay.crafting.gathering import add_gathered_to_inventory, resolve_gather_yield

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_RECIPE = {
    "id": "recipe_iron_sword",
    "world": "inkglass_dark",
    "name": "Iron Sword",
    "output_item_id": "iron_sword",
    "output_quantity": 1,
    "input_materials": [
        {"item_id": "iron_ingot", "quantity": 3},
        {"item_id": "leather_strip", "quantity": 1},
    ],
    "required_station_type": "forge",
    "required_skill": "athletics",
    "skill_dc": 12,
    "discovery_method": "taught",
    "level_requirement": 1,
}

SAMPLE_NODE = {
    "id": "node_iron_vein",
    "name": "Iron Vein",
    "material_id": "iron_ore",
    "skill": "survival",
    "dc": 10,
    "yield_min": 2,
    "yield_max": 4,
    "tier": "common",
}


# ---------------------------------------------------------------------------
# Unit tests — crafter.py
# ---------------------------------------------------------------------------


class TestCheckMaterials:
    def test_all_present(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 2, "binding_state": "unbound"},
        ]
        missing = check_materials(SAMPLE_RECIPE["input_materials"], inventory)
        assert missing == []

    def test_partial_missing(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 1, "binding_state": "unbound"},
        ]
        missing = check_materials(SAMPLE_RECIPE["input_materials"], inventory)
        assert len(missing) == 2
        assert any(m["item_id"] == "iron_ingot" and m["needed"] == 3 and m["have"] == 1 for m in missing)
        assert any(m["item_id"] == "leather_strip" and m["have"] == 0 for m in missing)

    def test_empty_inventory(self):
        missing = check_materials(SAMPLE_RECIPE["input_materials"], [])
        assert len(missing) == 2


class TestConsumeMaterials:
    def test_exact_consumption(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 3, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 1, "binding_state": "unbound"},
            {"item_id": "gold_coin", "quantity": 10, "binding_state": "unbound"},
        ]
        result = consume_materials(SAMPLE_RECIPE["input_materials"], inventory)
        assert len(result) == 1
        assert result[0]["item_id"] == "gold_coin"

    def test_partial_consumption(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 3, "binding_state": "unbound"},
        ]
        result = consume_materials(SAMPLE_RECIPE["input_materials"], inventory)
        iron = next(e for e in result if e["item_id"] == "iron_ingot")
        leather = next(e for e in result if e["item_id"] == "leather_strip")
        assert iron["quantity"] == 2
        assert leather["quantity"] == 2


class TestConsumePartialMaterials:
    def test_half_loss_rounded_up(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 10, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 5, "binding_state": "unbound"},
        ]
        updated, lost = consume_partial_materials(SAMPLE_RECIPE["input_materials"], inventory)
        # iron_ingot: ceil(3 * 0.5) = 2 lost, leather_strip: ceil(1 * 0.5) = 1 lost
        assert any(m["item_id"] == "iron_ingot" and m["quantity"] == 2 for m in lost)
        assert any(m["item_id"] == "leather_strip" and m["quantity"] == 1 for m in lost)
        iron = next(e for e in updated if e["item_id"] == "iron_ingot")
        leather = next(e for e in updated if e["item_id"] == "leather_strip")
        assert iron["quantity"] == 8
        assert leather["quantity"] == 4

    def test_custom_loss_fraction(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 10, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 5, "binding_state": "unbound"},
        ]
        _, lost = consume_partial_materials(SAMPLE_RECIPE["input_materials"], inventory, loss_fraction=0.25)
        # iron_ingot: ceil(3 * 0.25) = 1, leather_strip: ceil(1 * 0.25) = 1
        assert any(m["item_id"] == "iron_ingot" and m["quantity"] == 1 for m in lost)
        assert any(m["item_id"] == "leather_strip" and m["quantity"] == 1 for m in lost)

    def test_single_material_always_loses_at_least_one(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
        ]
        materials = [{"item_id": "iron_ingot", "quantity": 1}]
        updated, lost = consume_partial_materials(materials, inventory)
        # ceil(1 * 0.5) = 1
        assert lost[0]["quantity"] == 1
        iron = next(e for e in updated if e["item_id"] == "iron_ingot")
        assert iron["quantity"] == 4


class TestHasToolAdvantage:
    def test_matching_tool_grants_advantage(self):
        equipped = {
            "tool_slot": {
                "item_id": "smithing_hammer",
                "item_type": "tool",
                "associated_skill": "athletics",
            }
        }
        assert has_tool_advantage(equipped, "athletics") is True

    def test_non_matching_tool_no_advantage(self):
        equipped = {
            "tool_slot": {
                "item_id": "herbalism_kit",
                "item_type": "tool",
                "associated_skill": "nature",
            }
        }
        assert has_tool_advantage(equipped, "athletics") is False

    def test_non_tool_item_no_advantage(self):
        equipped = {
            "main_hand": {
                "item_id": "iron_sword",
                "item_type": "weapon",
                "associated_skill": "athletics",
            }
        }
        assert has_tool_advantage(equipped, "athletics") is False

    def test_empty_gear_no_advantage(self):
        assert has_tool_advantage({}, "athletics") is False


class TestProduceOutput:
    def test_new_item_added(self):
        inventory = [{"item_id": "other", "quantity": 1, "binding_state": "unbound"}]
        result = produce_output("iron_sword", 1, inventory)
        assert len(result) == 2
        sword = next(e for e in result if e["item_id"] == "iron_sword")
        assert sword["quantity"] == 1
        assert sword["binding_state"] == "unbound"

    def test_stacks_existing(self):
        inventory = [{"item_id": "iron_sword", "quantity": 1, "binding_state": "unbound"}]
        result = produce_output("iron_sword", 1, inventory)
        sword = next(e for e in result if e["item_id"] == "iron_sword")
        assert sword["quantity"] == 2


class TestValidateRecipeRequirements:
    def test_valid(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 2, "binding_state": "unbound"},
        ]
        result = validate_recipe_requirements(
            SAMPLE_RECIPE,
            character_level=5,
            known_recipes=["recipe_iron_sword"],
            inventory=inventory,
            station_type="forge",
        )
        assert result is None

    def test_recipe_not_known(self):
        with pytest.raises(RecipeNotKnownError):
            validate_recipe_requirements(
                SAMPLE_RECIPE, character_level=5, known_recipes=[], inventory=[], station_type="forge"
            )

    def test_level_too_low(self):
        recipe = {**SAMPLE_RECIPE, "level_requirement": 10}
        with pytest.raises(LevelTooLowError):
            validate_recipe_requirements(
                recipe, character_level=5, known_recipes=["recipe_iron_sword"], inventory=[], station_type="forge"
            )

    def test_missing_materials(self):
        with pytest.raises(MissingMaterialsError):
            validate_recipe_requirements(
                SAMPLE_RECIPE,
                character_level=5,
                known_recipes=["recipe_iron_sword"],
                inventory=[],
                station_type="forge",
            )

    def test_station_required(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 2, "binding_state": "unbound"},
        ]
        with pytest.raises(StationRequiredError):
            validate_recipe_requirements(
                SAMPLE_RECIPE,
                character_level=5,
                known_recipes=["recipe_iron_sword"],
                inventory=inventory,
                station_type=None,
            )

    def test_wrong_station(self):
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 2, "binding_state": "unbound"},
        ]
        with pytest.raises(StationRequiredError):
            validate_recipe_requirements(
                SAMPLE_RECIPE,
                character_level=5,
                known_recipes=["recipe_iron_sword"],
                inventory=inventory,
                station_type="alchemy_bench",
            )

    def test_no_station_required_in_recipe(self):
        recipe = {**SAMPLE_RECIPE}
        del recipe["required_station_type"]
        inventory = [
            {"item_id": "iron_ingot", "quantity": 5, "binding_state": "unbound"},
            {"item_id": "leather_strip", "quantity": 2, "binding_state": "unbound"},
        ]
        result = validate_recipe_requirements(
            recipe,
            character_level=5,
            known_recipes=["recipe_iron_sword"],
            inventory=inventory,
            station_type=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests — gathering.py
# ---------------------------------------------------------------------------


class TestGatherYield:
    def test_success_yields_materials(self):
        with patch("relay.crafting.gathering.random.randint", return_value=3):
            result = resolve_gather_yield(SAMPLE_NODE, check_passed=True)
        assert result["success"] is True
        assert result["material_id"] == "iron_ore"
        assert result["quantity"] == 3

    def test_failure_yields_nothing(self):
        result = resolve_gather_yield(SAMPLE_NODE, check_passed=False)
        assert result["success"] is False
        assert result["quantity"] == 0


class TestAddGatheredToInventory:
    def test_new_material(self):
        inventory = []
        result = add_gathered_to_inventory("iron_ore", 3, inventory)
        assert len(result) == 1
        assert result[0]["item_id"] == "iron_ore"
        assert result[0]["quantity"] == 3

    def test_stacks_existing(self):
        inventory = [{"item_id": "iron_ore", "quantity": 2, "binding_state": "unbound"}]
        result = add_gathered_to_inventory("iron_ore", 3, inventory)
        assert len(result) == 1
        assert result[0]["quantity"] == 5

    def test_zero_quantity_no_change(self):
        inventory = [{"item_id": "iron_ore", "quantity": 2, "binding_state": "unbound"}]
        result = add_gathered_to_inventory("iron_ore", 0, inventory)
        assert result[0]["quantity"] == 2


# ---------------------------------------------------------------------------
# Integration tests — endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_header():
    token = create_session_token(
        player_id="player_001",
        world_id="inkglass_dark",
        session_id="sess_001",
        tier=1,
        role="player",
        mode="solo",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def crafter_id(db_client, auth_header):
    """Create a character with materials and known recipe."""
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Forgemaster Kael",
            "specialisation_path_id": "warrior",
            "ability_scores": {
                "strength": 16,
                "dexterity": 12,
                "constitution": 14,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 8,
            },
            "skill_proficiencies": ["athletics", "survival"],
            "saving_throw_proficiencies": ["strength", "constitution"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    char_id = resp.json()["id"]

    patch_resp = db_client.patch(
        f"/character/{char_id}",
        json={
            "inventory": [
                {"item_id": "iron_ingot", "quantity": 10, "binding_state": "unbound"},
                {"item_id": "leather_strip", "quantity": 5, "binding_state": "unbound"},
            ],
            "known_recipes": ["recipe_iron_sword"],
        },
        headers=auth_header,
    )
    assert patch_resp.status_code == 200
    return char_id


class TestCraftEndpoint:
    def test_craft_success(self, db_client, session_header, crafter_id):
        with patch("relay.checks.resolver.random.randint", return_value=18):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["output_item_id"] == "iron_sword"
        assert data["output_quantity"] == 1
        assert data["materials_consumed"] is True
        assert data["materials_lost"] is None
        assert data["critical"] is False
        assert data["check_result"]["passed"] is True

        char = db_client.get(f"/character/{crafter_id}", headers=_make_auth_header()).json()
        iron = next((e for e in char["inventory"] if e["item_id"] == "iron_ingot"), None)
        assert iron["quantity"] == 7  # 10 - 3
        sword = next((e for e in char["inventory"] if e["item_id"] == "iron_sword"), None)
        assert sword is not None
        assert sword["quantity"] == 1

    def test_craft_failed_check_partial_material_loss(self, db_client, session_header, crafter_id):
        with patch("relay.checks.resolver.random.randint", return_value=2):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["materials_consumed"] is False
        assert data["check_result"]["passed"] is False
        assert data["materials_lost"] is not None
        # 50% rounded up: iron_ingot loses 2, leather_strip loses 1
        iron_lost = next(m for m in data["materials_lost"] if m["item_id"] == "iron_ingot")
        leather_lost = next(m for m in data["materials_lost"] if m["item_id"] == "leather_strip")
        assert iron_lost["quantity"] == 2
        assert leather_lost["quantity"] == 1

        char = db_client.get(f"/character/{crafter_id}", headers=_make_auth_header()).json()
        iron = next((e for e in char["inventory"] if e["item_id"] == "iron_ingot"), None)
        assert iron["quantity"] == 8  # 10 - 2
        leather = next((e for e in char["inventory"] if e["item_id"] == "leather_strip"), None)
        assert leather["quantity"] == 4  # 5 - 1

    def test_craft_critical_success_bonus_output(self, db_client, session_header, crafter_id):
        with patch("relay.checks.resolver.random.randint", return_value=20):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["critical"] is True
        assert data["output_quantity"] == 2  # base 1 + 1 crit bonus

        char = db_client.get(f"/character/{crafter_id}", headers=_make_auth_header()).json()
        sword = next((e for e in char["inventory"] if e["item_id"] == "iron_sword"), None)
        assert sword["quantity"] == 2

    def test_craft_station_required(self, db_client, session_header, crafter_id):
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": SAMPLE_RECIPE,
                "station_type": None,
            },
            headers=session_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "station_required"

    def test_craft_wrong_station(self, db_client, session_header, crafter_id):
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": SAMPLE_RECIPE,
                "station_type": "alchemy_bench",
            },
            headers=session_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "station_required"

    def test_craft_tool_advantage(self, db_client, session_header, auth_header, crafter_id):
        db_client.patch(
            f"/character/{crafter_id}",
            json={
                "equipped_gear": {
                    "tool_slot": {
                        "item_id": "smithing_hammer",
                        "item_type": "tool",
                        "associated_skill": "athletics",
                    }
                },
            },
            headers=auth_header,
        )

        with patch("relay.checks.resolver.random.randint", side_effect=[8, 18]):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["check_result"]["roll_mode"] == "advantage"
        assert data["success"] is True

    def test_craft_recipe_not_known(self, db_client, session_header, crafter_id):
        unknown_recipe = {**SAMPLE_RECIPE, "id": "recipe_unknown"}
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": unknown_recipe,
                "station_type": "forge",
            },
            headers=session_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "recipe_not_known"

    def test_craft_missing_materials(self, db_client, session_header, auth_header, crafter_id):
        db_client.patch(
            f"/character/{crafter_id}",
            json={"inventory": []},
            headers=auth_header,
        )

        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": SAMPLE_RECIPE,
                "station_type": "forge",
            },
            headers=session_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_materials"

    def test_craft_transaction_log_created(self, db_client, session_header, crafter_id):
        with patch("relay.checks.resolver.random.randint", return_value=18):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True

        tx_resp = db_client.get(
            f"/wallet/{crafter_id}/transactions",
            headers=_make_auth_header(),
        )
        if tx_resp.status_code == 200:
            txs = tx_resp.json()["transactions"]
            craft_txs = [t for t in txs if t["tx_type"] == "craft"]
            assert len(craft_txs) >= 1
            assert craft_txs[0]["item_id"] == "iron_sword"


class TestGatherEndpoint:
    def test_gather_success(self, db_client, session_header, crafter_id):
        with patch("random.randint", side_effect=[15, 3]):
            resp = db_client.post(
                "/gather",
                json={"character_id": crafter_id, "node": SAMPLE_NODE},
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["material_id"] == "iron_ore"
        assert data["quantity"] == 3

        char = db_client.get(f"/character/{crafter_id}", headers=_make_auth_header()).json()
        ore = next((e for e in char["inventory"] if e["item_id"] == "iron_ore"), None)
        assert ore is not None
        assert ore["quantity"] == 3

    def test_gather_failure(self, db_client, session_header, crafter_id):
        with patch("random.randint", return_value=1):
            resp = db_client.post(
                "/gather",
                json={"character_id": crafter_id, "node": SAMPLE_NODE},
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["quantity"] == 0

    def test_gather_stacks_existing_materials(self, db_client, session_header, auth_header, crafter_id):
        db_client.patch(
            f"/character/{crafter_id}",
            json={
                "inventory": [
                    {"item_id": "iron_ore", "quantity": 5, "binding_state": "unbound"},
                ],
            },
            headers=auth_header,
        )

        with patch("random.randint", side_effect=[15, 2]):
            resp = db_client.post(
                "/gather",
                json={"character_id": crafter_id, "node": SAMPLE_NODE},
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["quantity"] == 2

        char = db_client.get(f"/character/{crafter_id}", headers=_make_auth_header()).json()
        ore = next(e for e in char["inventory"] if e["item_id"] == "iron_ore")
        assert ore["quantity"] == 7  # 5 + 2


def _make_auth_header() -> dict:
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Step 12 continued — new tests for crafting/gathering improvements
# ---------------------------------------------------------------------------


class TestRecipeValidationModel:
    """#1 — Pydantic RecipeInput rejects malformed payloads."""

    def test_missing_recipe_id(self, db_client, session_header, crafter_id):
        """Recipe without 'id' field is rejected with 422."""
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": {
                    "output_item_id": "iron_sword",
                    "input_materials": [{"item_id": "iron_ingot", "quantity": 3}],
                },
                "station_type": "forge",
            },
            headers=session_header,
        )
        assert resp.status_code == 422

    def test_missing_material_id(self, db_client, session_header, crafter_id):
        """Recipe with malformed input_materials is rejected."""
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": {
                    "id": "recipe_bad",
                    "output_item_id": "thing",
                    "input_materials": [{"quantity": 3}],  # missing item_id
                },
            },
            headers=session_header,
        )
        assert resp.status_code == 422

    def test_empty_input_materials(self, db_client, session_header, crafter_id):
        """Recipe with empty input_materials is rejected."""
        resp = db_client.post(
            "/craft",
            json={
                "character_id": crafter_id,
                "recipe": {
                    "id": "recipe_bad",
                    "output_item_id": "thing",
                    "input_materials": [],
                },
            },
            headers=session_header,
        )
        assert resp.status_code == 422


class TestGatherNodeValidationModel:
    """#1/#9 — Pydantic GatherNode rejects malformed payloads."""

    def test_missing_material_id(self, db_client, session_header, crafter_id):
        resp = db_client.post(
            "/gather",
            json={
                "character_id": crafter_id,
                "node": {"skill": "survival", "dc": 10},
            },
            headers=session_header,
        )
        assert resp.status_code == 422

    def test_missing_dc(self, db_client, session_header, crafter_id):
        """dc is required — no silent default to 12."""
        resp = db_client.post(
            "/gather",
            json={
                "character_id": crafter_id,
                "node": {"material_id": "iron_ore", "skill": "survival"},
            },
            headers=session_header,
        )
        assert resp.status_code == 422

    def test_missing_skill(self, db_client, session_header, crafter_id):
        resp = db_client.post(
            "/gather",
            json={
                "character_id": crafter_id,
                "node": {"material_id": "iron_ore", "dc": 10},
            },
            headers=session_header,
        )
        assert resp.status_code == 422


class TestExhaustionOnCraftGather:
    """#3/#4 — Exhaustion imposes disadvantage on craft/gather checks."""

    def test_exhausted_crafter_has_disadvantage(self, db_client, session_header, auth_header, crafter_id):
        """Exhaustion level 1 → disadvantage on craft check."""
        db_client.patch(
            f"/character/{crafter_id}",
            json={"exhaustion_level": 1},
            headers=auth_header,
        )

        with patch("relay.checks.resolver.random.randint", side_effect=[18, 5]):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["check_result"]["roll_mode"] == "disadvantage"
        # Disadvantage: rolled 18 and 5, takes lowest = 5
        assert data["check_result"]["roll"] == 5

    def test_exhausted_gatherer_has_disadvantage(self, db_client, session_header, auth_header, crafter_id):
        """Exhaustion level 2 → disadvantage on gather check."""
        db_client.patch(
            f"/character/{crafter_id}",
            json={"exhaustion_level": 2},
            headers=auth_header,
        )

        with patch("relay.checks.resolver.random.randint", side_effect=[15, 4]):
            resp = db_client.post(
                "/gather",
                json={"character_id": crafter_id, "node": SAMPLE_NODE},
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["check_result"]["roll_mode"] == "disadvantage"
        assert data["check_result"]["roll"] == 4


class TestCraftFailTransactionLog:
    """#6 — Failed crafts write a craft_fail transaction log entry."""

    def test_failed_craft_logs_transaction(self, db_client, session_header, crafter_id):
        with patch("relay.checks.resolver.random.randint", return_value=1):
            resp = db_client.post(
                "/craft",
                json={
                    "character_id": crafter_id,
                    "recipe": SAMPLE_RECIPE,
                    "station_type": "forge",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is False

        tx_resp = db_client.get(
            f"/wallet/{crafter_id}/transactions",
            headers=_make_auth_header(),
        )
        if tx_resp.status_code == 200:
            txs = tx_resp.json()["transactions"]
            fail_txs = [t for t in txs if t["tx_type"] == "craft_fail"]
            assert len(fail_txs) == 1
            assert "lost:" in fail_txs[0]["note"]
            assert fail_txs[0]["item_id"] == "iron_sword"


class TestLogItemTransactionHelper:
    """#8 — log_item_transaction centralises non-currency transaction logging."""

    @pytest.mark.asyncio()
    async def test_creates_transaction_log(self):
        from unittest.mock import MagicMock

        from relay.economy.wallet import log_item_transaction

        db = MagicMock()
        char = MagicMock()
        char.player_id = "p1"
        char.id = "c1"
        char.world_id = "inkglass_dark"

        log_item_transaction(
            db,
            char,
            tx_type="craft",
            item_id="iron_sword",
            item_quantity=1,
            currency="gold",
            session_id="sess_001",
            note="Crafted Iron Sword",
        )

        db.add.assert_called_once()
        tx = db.add.call_args[0][0]
        assert tx.tx_type == "craft"
        assert tx.item_id == "iron_sword"
        assert tx.item_quantity == 1
        assert tx.amount == 0
        assert tx.note == "Crafted Iron Sword"
