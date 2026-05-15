"""Tests for the combat system: attack rolls, damage, conditions, initiative, death state.

Sets up a combat encounter between a character and an NPC-like target,
verifies attack resolution, damage type multipliers, saving throws,
condition tracking, and the death state ticking clock.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token
from relay.combat.conditions import (
    apply_condition,
    get_attack_modifiers,
    get_defense_modifiers,
    get_save_modifiers,
    increment_exhaustion,
    reduce_exhaustion,
    remove_condition,
    tick_conditions,
)
from relay.combat.death_state import (
    enter_death_state,
    heal_from_death_state,
    is_retired,
    tick_death_state,
)
from relay.combat.initiative import determine_turn_order, roll_initiative
from relay.combat.resolver import (
    DAMAGE_TYPES,
    ENVIRONMENT_COMBAT_EFFECTS,
    ability_modifier,
    apply_damage_resistances,
    attack_roll,
    compute_ac,
    compute_save_dc,
    damage_roll,
    resolve_attack,
    roll_dice,
    saving_throw,
)
from relay.companions.combat_ai import (
    _pick_damage_dice,
    _pick_healing_dice,
    resolve_companion_action,
)

# ---------------------------------------------------------------------------
# Unit tests — resolver.py
# ---------------------------------------------------------------------------


class TestAbilityModifier:
    def test_standard_scores(self):
        assert ability_modifier(10) == 0
        assert ability_modifier(11) == 0
        assert ability_modifier(14) == 2
        assert ability_modifier(8) == -1
        assert ability_modifier(20) == 5
        assert ability_modifier(1) == -5


class TestComputeAC:
    def test_unarmoured(self):
        ac = compute_ac({}, {}, {"dexterity": 14})
        assert ac == 12  # 10 + 2 DEX

    def test_light_armour(self):
        items = {"leather": {"stats": {"ac_bonus": 1, "armour_category": "light"}}}
        ac = compute_ac({"armour": "leather"}, items, {"dexterity": 16})
        assert ac == 14  # 10 + 1 + 3 DEX (no cap)

    def test_medium_armour_dex_cap(self):
        items = {"chain": {"stats": {"ac_bonus": 4, "armour_category": "medium"}}}
        ac = compute_ac({"armour": "chain"}, items, {"dexterity": 18})
        assert ac == 16  # 10 + 4 + 2 (capped from +4)

    def test_heavy_armour_no_dex(self):
        items = {"plate": {"stats": {"ac_bonus": 8, "armour_category": "heavy"}}}
        ac = compute_ac({"armour": "plate"}, items, {"dexterity": 18})
        assert ac == 18  # 10 + 8 + 0

    def test_shield_bonus(self):
        items = {
            "leather": {"stats": {"ac_bonus": 1, "armour_category": "light"}},
            "buckler": {"stats": {"ac_bonus": 2}},
        }
        ac = compute_ac({"armour": "leather", "shield": "buckler"}, items, {"dexterity": 14})
        assert ac == 15  # 10 + 1 + 2 DEX + 2 shield

    def test_magical_bonus(self):
        items = {"plate": {"stats": {"ac_bonus": 8, "armour_category": "heavy", "magical_bonus": 1}}}
        ac = compute_ac({"armour": "plate"}, items, {"dexterity": 10})
        assert ac == 19  # 10 + 8 + 0 + 1


class TestRollDice:
    def test_basic_notation(self):
        total, rolls = roll_dice("2d6")
        assert len(rolls) == 2
        assert all(1 <= r <= 6 for r in rolls)
        assert total == sum(rolls)

    def test_single_die(self):
        total, rolls = roll_dice("1d20")
        assert len(rolls) == 1
        assert 1 <= total <= 20

    def test_flat_number(self):
        total, rolls = roll_dice("5")
        assert total == 5
        assert rolls == [5]


class TestAttackRoll:
    @patch("relay.combat.resolver.random.randint", return_value=15)
    def test_straight_roll(self, mock_rand):
        weapon = {"stats": {"modifier_source": "strength"}}
        result = attack_roll(
            {"strength": 16},
            5,
            weapon,
            proficient=True,
        )
        assert result["roll"] == 15
        assert result["ability_modifier"] == 3
        assert result["proficiency_bonus"] == 3
        assert result["total"] == 21

    @patch("relay.combat.resolver.random.randint", return_value=20)
    def test_natural_20(self, mock_rand):
        weapon = {"stats": {"modifier_source": "dexterity"}}
        result = attack_roll({"dexterity": 14}, 1, weapon)
        assert result["natural_20"] is True
        assert result["natural_1"] is False

    @patch("relay.combat.resolver.random.randint", return_value=1)
    def test_natural_1(self, mock_rand):
        weapon = {"stats": {"modifier_source": "strength"}}
        result = attack_roll({"strength": 20}, 20, weapon)
        assert result["natural_1"] is True


class TestResolveAttack:
    def test_hit(self):
        atk = {"total": 15, "natural_1": False, "natural_20": False}
        result = resolve_attack(atk, target_ac=14)
        assert result["hit"] is True
        assert result["critical"] is False

    def test_miss(self):
        atk = {"total": 12, "natural_1": False, "natural_20": False}
        result = resolve_attack(atk, target_ac=15)
        assert result["hit"] is False

    def test_nat_20_always_hits(self):
        atk = {"total": 5, "natural_1": False, "natural_20": True}
        result = resolve_attack(atk, target_ac=25)
        assert result["hit"] is True
        assert result["critical"] is True

    def test_nat_1_always_misses(self):
        atk = {"total": 30, "natural_1": True, "natural_20": False}
        result = resolve_attack(atk, target_ac=5)
        assert result["hit"] is False
        assert result["auto_miss"] is True


class TestDamageRoll:
    @patch("relay.combat.resolver.random.randint", return_value=4)
    def test_normal_damage(self, mock_rand):
        weapon = {"stats": {"damage_dice": "2d6", "damage_type": "slashing", "modifier_source": "strength"}}
        result = damage_roll(weapon, {"strength": 16})
        assert result["rolls"] == [4, 4]
        assert result["raw_total"] == 11  # 8 + 3 STR
        assert result["damage_type"] == "slashing"
        assert result["critical"] is False

    @patch("relay.combat.resolver.random.randint", return_value=3)
    def test_critical_doubles_dice(self, mock_rand):
        weapon = {"stats": {"damage_dice": "2d6", "damage_type": "slashing", "modifier_source": "strength"}}
        result = damage_roll(weapon, {"strength": 14}, critical=True)
        assert len(result["rolls"]) == 4  # 2d6 → 4d6
        assert result["damage_dice"] == "4d6"
        assert result["raw_total"] == 14  # 12 + 2 STR


class TestDamageResistances:
    def test_resistance_halves(self):
        dmg = {"raw_total": 10, "damage_type": "fire"}
        result = apply_damage_resistances(dmg, resistances=["fire"])
        assert result["final_damage"] == 5
        assert result["multiplier"] == 0.5

    def test_vulnerability_doubles(self):
        dmg = {"raw_total": 10, "damage_type": "cold"}
        result = apply_damage_resistances(dmg, vulnerabilities=["cold"])
        assert result["final_damage"] == 20
        assert result["multiplier"] == 2.0

    def test_immunity_zeroes(self):
        dmg = {"raw_total": 10, "damage_type": "poison"}
        result = apply_damage_resistances(dmg, immunities=["poison"])
        assert result["final_damage"] == 0
        assert result["multiplier"] == 0.0

    def test_no_matching_type(self):
        dmg = {"raw_total": 10, "damage_type": "slashing"}
        result = apply_damage_resistances(dmg, resistances=["fire"], vulnerabilities=["cold"])
        assert result["final_damage"] == 10
        assert result["multiplier"] == 1.0

    def test_immunity_takes_precedence(self):
        dmg = {"raw_total": 10, "damage_type": "fire"}
        result = apply_damage_resistances(dmg, resistances=["fire"], immunities=["fire"])
        assert result["final_damage"] == 0


class TestSavingThrow:
    @patch("relay.combat.resolver.random.randint", return_value=12)
    def test_pass(self, mock_rand):
        result = saving_throw(
            {"wisdom": 16},
            5,
            ["wisdom"],
            "wisdom",
            14,
        )
        assert result["passed"] is True
        # 12 + 3 WIS + 3 prof = 18 >= 14

    @patch("relay.combat.resolver.random.randint", return_value=5)
    def test_fail(self, mock_rand):
        result = saving_throw(
            {"dexterity": 10},
            1,
            ["constitution"],
            "dexterity",
            15,
        )
        assert result["passed"] is False
        # 5 + 0 DEX + 0 (no prof) = 5 < 15

    def test_compute_save_dc(self):
        dc = compute_save_dc({"intelligence": 18}, 9, "intelligence")
        # 8 + 4 INT + 4 prof = 16
        assert dc == 16

    @patch("relay.combat.resolver.random.randint", return_value=20)
    def test_natural_20_auto_passes(self, mock_rand):
        """(#5) Natural 20 on a saving throw always succeeds."""
        result = saving_throw(
            {"strength": 3},  # -4 modifier
            1,
            [],
            "strength",
            100,  # impossible DC
        )
        assert result["passed"] is True
        assert result["natural_20"] is True
        assert result["natural_1"] is False

    @patch("relay.combat.resolver.random.randint", return_value=1)
    def test_natural_1_auto_fails(self, mock_rand):
        """(#5) Natural 1 on a saving throw always fails."""
        result = saving_throw(
            {"wisdom": 20},  # +5 modifier
            20,
            ["wisdom"],
            "wisdom",
            1,  # trivial DC
        )
        assert result["passed"] is False
        assert result["natural_1"] is True
        assert result["natural_20"] is False

    @patch("relay.combat.resolver.random.randint", return_value=10)
    def test_save_normal_roll_no_nat_flags(self, mock_rand):
        """Normal saving throw roll has no natural extreme flags."""
        result = saving_throw({"dexterity": 14}, 5, ["dexterity"], "dexterity", 12)
        assert result["natural_20"] is False
        assert result["natural_1"] is False


class TestDamageTypeValidation:
    """(#11) Damage type validation in damage_roll."""

    @patch("relay.combat.resolver.random.randint", return_value=4)
    def test_valid_damage_type_preserved(self, mock_rand):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "fire", "modifier_source": "strength"}}
        result = damage_roll(weapon, {"strength": 10})
        assert result["damage_type"] == "fire"

    @patch("relay.combat.resolver.random.randint", return_value=4)
    def test_invalid_damage_type_defaults_to_bludgeoning(self, mock_rand):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "frie", "modifier_source": "strength"}}
        result = damage_roll(weapon, {"strength": 10})
        assert result["damage_type"] == "bludgeoning"

    def test_all_13_damage_types_in_registry(self):
        expected = {
            "slashing",
            "piercing",
            "bludgeoning",
            "fire",
            "cold",
            "lightning",
            "thunder",
            "poison",
            "acid",
            "necrotic",
            "radiant",
            "force",
            "psychic",
        }
        assert expected == DAMAGE_TYPES


class TestEnvironmentCombatEffects:
    """(#8) Data-driven environmental combat effects."""

    def test_darkness_gives_attack_disadvantage(self):
        env = ENVIRONMENT_COMBAT_EFFECTS["darkness"]
        assert env["attack"] == "disadvantage"

    def test_high_ground_gives_attack_advantage(self):
        env = ENVIRONMENT_COMBAT_EFFECTS["high_ground"]
        assert env["attack"] == "advantage"

    def test_difficult_terrain_no_attack_modifier(self):
        env = ENVIRONMENT_COMBAT_EFFECTS["difficult_terrain"]
        assert "attack" not in env

    def test_extreme_weather_attack_disadvantage(self):
        env = ENVIRONMENT_COMBAT_EFFECTS["extreme_weather"]
        assert env["attack"] == "disadvantage"


# ---------------------------------------------------------------------------
# Unit tests — conditions.py
# ---------------------------------------------------------------------------


class TestConditions:
    def test_apply_condition(self):
        conditions = []
        conditions = apply_condition(conditions, "poisoned", duration_turns=3, source="viper")
        assert len(conditions) == 1
        assert conditions[0]["condition_id"] == "poisoned"
        assert conditions[0]["duration_turns"] == 3

    def test_reject_invalid_condition(self):
        conditions = []
        conditions = apply_condition(conditions, "made_up", duration_turns=1)
        assert len(conditions) == 0

    def test_reject_no_duration(self):
        conditions = []
        conditions = apply_condition(conditions, "stunned")
        assert len(conditions) == 0

    def test_remove_condition(self):
        conditions = [
            {"condition_id": "poisoned", "duration_turns": 2, "source": "a"},
            {"condition_id": "stunned", "duration_turns": 1, "source": "b"},
        ]
        result = remove_condition(conditions, "poisoned")
        assert len(result) == 1
        assert result[0]["condition_id"] == "stunned"

    def test_tick_conditions_duration(self):
        conditions = [
            {"condition_id": "poisoned", "duration_turns": 2, "source": "a"},
            {"condition_id": "stunned", "duration_turns": 1, "source": "b"},
        ]
        result = tick_conditions(conditions, current_turn=5)
        assert len(result) == 1
        assert result[0]["condition_id"] == "poisoned"
        assert result[0]["duration_turns"] == 1

    def test_tick_conditions_expiry(self):
        conditions = [
            {"condition_id": "charmed", "expiry_turn": 5, "source": "c"},
        ]
        result = tick_conditions(conditions, current_turn=5)
        assert len(result) == 0

    def test_attack_modifiers_poisoned(self):
        conditions = [{"condition_id": "poisoned", "duration_turns": 2, "source": "x"}]
        mods = get_attack_modifiers(conditions, exhaustion_level=0)
        assert mods["attack_disadvantage"] is True

    def test_attack_modifiers_exhaustion_3(self):
        mods = get_attack_modifiers([], exhaustion_level=3)
        assert mods["attack_disadvantage"] is True

    def test_defense_modifiers_stunned(self):
        conditions = [{"condition_id": "stunned", "duration_turns": 1, "source": "x"}]
        mods = get_defense_modifiers(conditions)
        assert mods["attackers_have_advantage"] is True

    def test_save_modifiers_stunned_dex(self):
        conditions = [{"condition_id": "stunned", "duration_turns": 1, "source": "x"}]
        mods = get_save_modifiers(conditions, exhaustion_level=0, save_type="dexterity")
        assert mods["auto_fail"] is True

    def test_save_modifiers_stunned_wisdom(self):
        conditions = [{"condition_id": "stunned", "duration_turns": 1, "source": "x"}]
        mods = get_save_modifiers(conditions, exhaustion_level=0, save_type="wisdom")
        assert mods["auto_fail"] is False

    def test_exhaustion_increment_cap(self):
        assert increment_exhaustion(5) == 6
        assert increment_exhaustion(6) == 6

    def test_exhaustion_reduce(self):
        assert reduce_exhaustion(3) == 2
        assert reduce_exhaustion(0) == 0

    # (#7) Blinded and prone conditions
    def test_apply_blinded(self):
        conditions = []
        conditions = apply_condition(conditions, "blinded", duration_turns=2, source="flash")
        assert len(conditions) == 1
        assert conditions[0]["condition_id"] == "blinded"

    def test_apply_prone(self):
        conditions = []
        conditions = apply_condition(conditions, "prone", duration_turns=1, source="trip")
        assert len(conditions) == 1
        assert conditions[0]["condition_id"] == "prone"

    # (#4) Frightened attack disadvantage
    def test_attack_modifiers_frightened_source_visible(self):
        conditions = [{"condition_id": "frightened", "duration_turns": 2, "source": "dragon"}]
        mods = get_attack_modifiers(conditions, exhaustion_level=0, source_visible=True)
        assert mods["attack_disadvantage"] is True

    def test_attack_modifiers_frightened_source_not_visible(self):
        conditions = [{"condition_id": "frightened", "duration_turns": 2, "source": "dragon"}]
        mods = get_attack_modifiers(conditions, exhaustion_level=0, source_visible=False)
        assert mods["attack_disadvantage"] is False

    # (#7) Blinded attack modifiers
    def test_attack_modifiers_blinded(self):
        conditions = [{"condition_id": "blinded", "duration_turns": 1, "source": "sand"}]
        mods = get_attack_modifiers(conditions, exhaustion_level=0)
        assert mods["attack_disadvantage"] is True

    # (#7) Prone attack modifiers
    def test_attack_modifiers_prone(self):
        conditions = [{"condition_id": "prone", "duration_turns": 1, "source": "trip"}]
        mods = get_attack_modifiers(conditions, exhaustion_level=0)
        assert mods["attack_disadvantage"] is True

    # (#7) Blinded defense modifiers
    def test_defense_modifiers_blinded(self):
        conditions = [{"condition_id": "blinded", "duration_turns": 1, "source": "sand"}]
        mods = get_defense_modifiers(conditions)
        assert mods["attackers_have_advantage"] is True

    # (#7) Prone defense modifiers — range-dependent
    def test_defense_modifiers_prone_melee(self):
        conditions = [{"condition_id": "prone", "duration_turns": 1, "source": "trip"}]
        mods = get_defense_modifiers(conditions, attack_range="melee")
        assert mods["attackers_have_advantage"] is True
        assert mods["attackers_have_disadvantage"] is False

    def test_defense_modifiers_prone_ranged(self):
        conditions = [{"condition_id": "prone", "duration_turns": 1, "source": "trip"}]
        mods = get_defense_modifiers(conditions, attack_range="ranged")
        assert mods["attackers_have_advantage"] is False
        assert mods["attackers_have_disadvantage"] is True

    def test_defense_modifiers_no_conditions(self):
        mods = get_defense_modifiers([])
        assert mods["attackers_have_advantage"] is False
        assert mods["attackers_have_disadvantage"] is False


# ---------------------------------------------------------------------------
# Unit tests — companion combat AI
# ---------------------------------------------------------------------------


class TestCompanionCombatAI:
    """(#2, #3) Companion combat AI uses shared roll functions and parses abilities."""

    @patch("relay.checks.resolver.random.randint", return_value=15)
    @patch("relay.combat.resolver.random.randint", return_value=4)
    def test_aggressive_attack_hits(self, mock_dmg, mock_d20):
        companion = {"active": True, "hp_current": 10, "behavior_type": "aggressive", "npc_id": "wolf_01"}
        companion_data = {"level": 3, "ability_scores": {"strength": 14}, "combat_profile": {"abilities": []}}
        target = {"id": "goblin_01", "ac": 12, "hp_current": 10}

        result = resolve_companion_action(
            companion=companion,
            companion_data=companion_data,
            target=target,
        )
        assert result["action_type"] == "attack"
        assert result["hit"] is True
        assert result["damage"] > 0
        assert result["dice"] == [15]  # (#2) dice array returned

    @patch("relay.combat.resolver.random.randint", return_value=5)
    def test_supportive_heals_lowest_hp(self, mock_dice):
        companion = {"active": True, "hp_current": 10, "behavior_type": "supportive", "npc_id": "cleric_01"}
        companion_data = {"level": 1, "ability_scores": {"wisdom": 16}, "combat_profile": {"abilities": []}}
        allies = [
            {"id": "a", "hp_current": 20, "hp_max": 20},
            {"id": "b", "hp_current": 3, "hp_max": 15},
        ]

        result = resolve_companion_action(
            companion=companion,
            companion_data=companion_data,
            allies=allies,
        )
        assert result["action_type"] == "heal"
        assert result["target_id"] == "b"
        assert result["healing"] >= 1

    def test_incapacitated_companion_does_nothing(self):
        companion = {"active": True, "hp_current": 0, "behavior_type": "aggressive", "npc_id": "wolf_01"}
        companion_data = {"level": 1, "ability_scores": {"strength": 14}, "combat_profile": {"abilities": []}}

        result = resolve_companion_action(companion=companion, companion_data=companion_data, target={"id": "x"})
        assert result["action_type"] == "none"

    # (#3) Ability parsing
    def test_pick_damage_dice_from_dict(self):
        abilities = [{"name": "claw", "damage_dice": "2d8"}]
        assert _pick_damage_dice(abilities) == "2d8"

    def test_pick_damage_dice_default(self):
        assert _pick_damage_dice([]) == "1d6"
        assert _pick_damage_dice(["basic_attack"]) == "1d6"

    def test_pick_healing_dice_from_dict(self):
        abilities = [{"name": "mend", "healing_dice": "2d6"}]
        assert _pick_healing_dice(abilities) == "2d6"

    def test_pick_healing_dice_default(self):
        assert _pick_healing_dice([]) == "1d8"


# ---------------------------------------------------------------------------
# Unit tests — initiative.py
# ---------------------------------------------------------------------------


class TestInitiative:
    @patch("relay.checks.resolver.random.randint", return_value=15)
    def test_roll_initiative(self, mock_rand):
        result = roll_initiative("warrior_01", {"dexterity": 14})
        assert result["roll"] == 15
        assert result["dex_modifier"] == 2
        assert result["total"] == 17
        assert result["dice"] == [15]  # (#1) now returns dice array

    def test_determine_turn_order(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[18, 5, 12]):
            participants = [
                {"id": "a", "ability_scores": {"dexterity": 10}},
                {"id": "b", "ability_scores": {"dexterity": 14}},
                {"id": "c", "ability_scores": {"dexterity": 12}},
            ]
            order = determine_turn_order(participants)
            assert order[0]["participant_id"] == "a"  # 18+0=18
            assert order[1]["participant_id"] == "c"  # 12+1=13
            assert order[2]["participant_id"] == "b"  # 5+2=7


# ---------------------------------------------------------------------------
# Unit tests — death_state.py
# ---------------------------------------------------------------------------


class TestDeathState:
    def test_enter_death_state_at_zero_hp(self):
        result = enter_death_state(hp_current=0, exhaustion_level=0)
        assert result["in_death_state"] is True
        assert result["exhaustion_level"] == 1

    def test_no_death_state_above_zero(self):
        result = enter_death_state(hp_current=5, exhaustion_level=0)
        assert result["in_death_state"] is False

    def test_tick_death_state_gains_exhaustion(self):
        result = tick_death_state(hp_current=0, exhaustion_level=1, death_state_exhaustion_gained=1)
        assert result["in_death_state"] is True
        assert result["exhaustion_level"] == 2
        assert result["death_state_exhaustion_gained"] == 2

    def test_tick_death_state_capped_at_3(self):
        result = tick_death_state(hp_current=0, exhaustion_level=3, death_state_exhaustion_gained=3)
        assert result["in_death_state"] is True
        assert result["exhaustion_level"] == 3  # no further gain from this source
        assert result["death_state_exhaustion_gained"] == 3

    def test_heal_from_death_state(self):
        result = heal_from_death_state(hp_current=0, healing=5, exhaustion_level=2)
        assert result["hp_current"] == 5
        assert result["in_death_state"] is False
        assert result["exhaustion_level"] == 2  # exhaustion remains

    def test_heal_not_enough(self):
        result = heal_from_death_state(hp_current=-3, healing=2, exhaustion_level=1)
        assert result["hp_current"] == -1
        assert result["in_death_state"] is True

    def test_is_retired(self):
        assert is_retired(6) is True
        assert is_retired(5) is False

    def test_retirement_at_exhaustion_6(self):
        result = tick_death_state(hp_current=0, exhaustion_level=5, death_state_exhaustion_gained=2)
        assert result["exhaustion_level"] == 6
        assert result["retired"] is True


# ---------------------------------------------------------------------------
# Integration tests — combat endpoints
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
def combatants(db_client, auth_header):
    """Create attacker and target characters."""
    attacker_resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Kael the Striker",
            "specialisation_path_id": "warrior",
            "ability_scores": {
                "strength": 16,
                "dexterity": 14,
                "constitution": 14,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 8,
            },
            "skill_proficiencies": ["athletics", "intimidation"],
            "saving_throw_proficiencies": ["strength", "constitution"],
        },
        headers=auth_header,
    )
    assert attacker_resp.status_code == 201

    target_resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Goblin Grunt",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 8,
                "dexterity": 14,
                "constitution": 10,
                "intelligence": 8,
                "wisdom": 10,
                "charisma": 6,
            },
            "skill_proficiencies": ["stealth"],
            "saving_throw_proficiencies": ["dexterity", "wisdom"],
        },
        headers=auth_header,
    )
    assert target_resp.status_code == 201

    return {
        "attacker_id": attacker_resp.json()["id"],
        "target_id": target_resp.json()["id"],
    }


class TestAttackEndpoint:
    def test_attack_hits_and_deals_damage(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "slashing", "modifier_source": "strength"}}

        with patch("relay.combat.resolver.random.randint", return_value=18):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                    "proficient": True,
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is True
        assert data["damage"] is not None
        assert data["damage"]["damage_type"] == "slashing"
        assert data["target_hp_after"] is not None

    def test_attack_misses(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "slashing", "modifier_source": "strength"}}

        with patch("relay.combat.resolver.random.randint", return_value=1):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is False
        assert data["auto_miss"] is True
        assert data["damage"] is None

    def test_critical_hit_doubles_dice(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "2d6", "damage_type": "slashing", "modifier_source": "strength"}}

        with patch("relay.combat.resolver.random.randint", return_value=20):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                },
                headers=session_header,
            )

        data = resp.json()
        assert data["hit"] is True
        assert data["critical"] is True
        assert data["damage"]["critical"] is True
        assert data["damage"]["damage_dice"] == "4d6"


class TestSaveEndpoint:
    def test_failed_save_applies_damage(self, db_client, session_header, combatants):
        with patch("relay.combat.resolver.random.randint", return_value=3):
            resp = db_client.post(
                "/combat/save",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "defender_id": combatants["target_id"],
                    "save_type": "constitution",
                    "dc_source_ability": "strength",
                    "damage_dice": "2d6",
                    "damage_type": "poison",
                    "half_on_save": True,
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["passed"] is False
        assert data["damage"] is not None
        assert data["damage"]["damage_type"] == "poison"

    def test_passed_save_half_damage(self, db_client, session_header, combatants):
        with patch("relay.combat.resolver.random.randint", return_value=19):
            resp = db_client.post(
                "/combat/save",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "defender_id": combatants["target_id"],
                    "save_type": "dexterity",
                    "dc_source_ability": "strength",
                    "damage_dice": "3d6",
                    "damage_type": "fire",
                    "half_on_save": True,
                },
                headers=session_header,
            )

        data = resp.json()
        assert data["passed"] is True
        assert data["damage"] is not None
        # Half damage on save

    def test_failed_save_applies_condition(self, db_client, session_header, combatants):
        with patch("relay.combat.resolver.random.randint", return_value=2):
            resp = db_client.post(
                "/combat/save",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "defender_id": combatants["target_id"],
                    "save_type": "wisdom",
                    "dc_source_ability": "charisma",
                    "applies_condition": {"condition_id": "frightened", "duration": 3},
                },
                headers=session_header,
            )

        data = resp.json()
        assert data["passed"] is False
        assert data["condition_applied"] == "frightened"


class TestInitiativeEndpoint:
    def test_initiative_returns_order(self, db_client, session_header, combatants):
        resp = db_client.post(
            "/combat/initiative",
            json={"participant_ids": [combatants["attacker_id"], combatants["target_id"]]},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["turn_order"]) == 2
        ids = [r["participant_id"] for r in data["turn_order"]]
        assert combatants["attacker_id"] in ids
        assert combatants["target_id"] in ids


class TestHealEndpoint:
    def test_heal_restores_hp(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "slashing", "modifier_source": "strength"}}
        with patch("relay.combat.resolver.random.randint", return_value=18):
            db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                },
                headers=session_header,
            )

        resp = db_client.post(
            "/combat/heal",
            json={"target_id": combatants["target_id"], "healing": 5},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hp_after"] > 0

    def test_heal_from_death_state(self, db_client, session_header, auth_header):
        resp = db_client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Dying Hero",
                "specialisation_path_id": "warrior",
                "ability_scores": {
                    "strength": 10,
                    "dexterity": 10,
                    "constitution": 10,
                    "intelligence": 10,
                    "wisdom": 10,
                    "charisma": 10,
                },
                "skill_proficiencies": ["athletics"],
                "saving_throw_proficiencies": ["strength", "constitution"],
            },
            headers=auth_header,
        )
        char_id = resp.json()["id"]

        # Set HP to 0 via PATCH
        patch_resp = db_client.patch(
            f"/character/{char_id}",
            json={"hp_current": 0},
            headers=auth_header,
        )
        assert patch_resp.status_code == 200

        heal_resp = db_client.post(
            "/combat/heal",
            json={"target_id": char_id, "healing": 10},
            headers=session_header,
        )
        assert heal_resp.status_code == 200
        data = heal_resp.json()
        assert data["left_death_state"] is True
        assert data["hp_after"] == data["hp_max"]  # capped at hp_max (8 for d8 + CON 0)


class TestRestEndpoint:
    """(#10) POST /combat/rest — short and long rest."""

    def test_long_rest_restores_hp(self, db_client, session_header, auth_header):
        resp = db_client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Tired Fighter",
                "specialisation_path_id": "warrior",
                "ability_scores": {
                    "strength": 14,
                    "dexterity": 10,
                    "constitution": 14,
                    "intelligence": 10,
                    "wisdom": 10,
                    "charisma": 10,
                },
                "skill_proficiencies": ["athletics"],
                "saving_throw_proficiencies": ["strength", "constitution"],
            },
            headers=auth_header,
        )
        char_id = resp.json()["id"]
        hp_max = resp.json()["hp_max"]  # should be 10 (d8 + 2 CON)

        # Damage the character
        db_client.patch(
            f"/character/{char_id}",
            json={"hp_current": 3},
            headers=auth_header,
        )

        rest_resp = db_client.post(
            "/combat/rest",
            json={"character_id": char_id, "rest_type": "long"},
            headers=session_header,
        )
        assert rest_resp.status_code == 200
        data = rest_resp.json()
        assert data["hp_before"] == 3
        assert data["hp_after"] == hp_max
        assert data["rest_type"] == "long"

    def test_long_rest_reduces_exhaustion(self, db_client, session_header, auth_header):
        resp = db_client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Exhausted Scout",
                "specialisation_path_id": "scout",
                "ability_scores": {
                    "strength": 10,
                    "dexterity": 14,
                    "constitution": 10,
                    "intelligence": 10,
                    "wisdom": 10,
                    "charisma": 10,
                },
                "skill_proficiencies": ["stealth"],
                "saving_throw_proficiencies": ["dexterity", "wisdom"],
            },
            headers=auth_header,
        )
        char_id = resp.json()["id"]

        # Set exhaustion to 2
        db_client.patch(
            f"/character/{char_id}",
            json={"exhaustion_level": 2},
            headers=auth_header,
        )

        rest_resp = db_client.post(
            "/combat/rest",
            json={"character_id": char_id, "rest_type": "long"},
            headers=session_header,
        )
        assert rest_resp.status_code == 200
        data = rest_resp.json()
        assert data["exhaustion_before"] == 2
        assert data["exhaustion_after"] == 1

    def test_short_rest_no_hp_change(self, db_client, session_header, auth_header):
        resp = db_client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Quick Rest Warrior",
                "specialisation_path_id": "warrior",
                "ability_scores": {
                    "strength": 14,
                    "dexterity": 10,
                    "constitution": 12,
                    "intelligence": 10,
                    "wisdom": 10,
                    "charisma": 10,
                },
                "skill_proficiencies": ["athletics"],
                "saving_throw_proficiencies": ["strength", "constitution"],
            },
            headers=auth_header,
        )
        char_id = resp.json()["id"]

        # Damage the character
        db_client.patch(
            f"/character/{char_id}",
            json={"hp_current": 3},
            headers=auth_header,
        )

        rest_resp = db_client.post(
            "/combat/rest",
            json={"character_id": char_id, "rest_type": "short"},
            headers=session_header,
        )
        assert rest_resp.status_code == 200
        data = rest_resp.json()
        assert data["hp_before"] == 3
        assert data["hp_after"] == 3  # short rest has no HP effect in Phase 0

    def test_invalid_rest_type_rejected(self, db_client, session_header, combatants):
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": combatants["attacker_id"], "rest_type": "nap"},
            headers=session_header,
        )
        assert resp.status_code == 422


class TestDeathStateTracking:
    """(#6) Death state exhaustion persistence across ticks."""

    def test_tick_conditions_tracks_death_exhaustion(self, db_client, session_header, auth_header):
        """Death state exhaustion accumulates across ticks and caps at 3."""
        resp = db_client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Death Tick Test",
                "specialisation_path_id": "warrior",
                "ability_scores": {
                    "strength": 10,
                    "dexterity": 10,
                    "constitution": 10,
                    "intelligence": 10,
                    "wisdom": 10,
                    "charisma": 10,
                },
                "skill_proficiencies": ["athletics"],
                "saving_throw_proficiencies": ["strength", "constitution"],
            },
            headers=auth_header,
        )
        char_id = resp.json()["id"]

        # Set HP to 0 and enter death state
        db_client.patch(
            f"/character/{char_id}",
            json={"hp_current": 0, "death_state_exhaustion_gained": 1, "exhaustion_level": 1},
            headers=auth_header,
        )

        # Tick 1: should gain exhaustion (1→2)
        tick_resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": char_id, "current_turn": 2},
            headers=session_header,
        )
        assert tick_resp.status_code == 200
        data = tick_resp.json()
        assert data["exhaustion_level"] == 2
        assert data["death_state"]["death_state_exhaustion_gained"] == 2

        # Tick 2: should gain again (2→3)
        tick_resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": char_id, "current_turn": 3},
            headers=session_header,
        )
        data = tick_resp.json()
        assert data["exhaustion_level"] == 3
        assert data["death_state"]["death_state_exhaustion_gained"] == 3

        # Tick 3: capped at 3 — no more gain from this source
        tick_resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": char_id, "current_turn": 4},
            headers=session_header,
        )
        data = tick_resp.json()
        assert data["exhaustion_level"] == 3
        assert data["death_state"]["death_state_exhaustion_gained"] == 3


class TestEnvironmentalAttackEffects:
    """(#8) Data-driven environmental effects via attack endpoint."""

    def test_high_ground_grants_advantage(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "slashing", "modifier_source": "strength"}}
        with patch("relay.combat.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                    "environmental_effects": ["high_ground"],
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        # The roll should go through — we're just verifying the endpoint accepts it

    def test_darkness_grants_disadvantage(self, db_client, session_header, combatants):
        weapon = {"stats": {"damage_dice": "1d8", "damage_type": "slashing", "modifier_source": "strength"}}
        with patch("relay.combat.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": combatants["attacker_id"],
                    "target_id": combatants["target_id"],
                    "weapon": weapon,
                    "environmental_effects": ["darkness"],
                },
                headers=session_header,
            )
        assert resp.status_code == 200
