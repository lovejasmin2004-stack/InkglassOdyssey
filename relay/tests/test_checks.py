"""Tests for the check system: validation, advantage/disadvantage, passive checks."""
from __future__ import annotations

import random
from unittest.mock import patch

import pytest

from relay.checks.resolver import (
    VALID_SKILLS,
    ability_modifier,
    compute_passive_check,
    determine_roll_mode,
    evaluate_passive_checks,
    proficiency_bonus,
    resolve_check,
    validate_check,
)


# ---------------------------------------------------------------------------
# Ability modifier and proficiency bonus
# ---------------------------------------------------------------------------

class TestAbilityModifier:
    def test_standard_values(self):
        assert ability_modifier(10) == 0
        assert ability_modifier(11) == 0
        assert ability_modifier(12) == 1
        assert ability_modifier(14) == 2
        assert ability_modifier(16) == 3
        assert ability_modifier(20) == 5
        assert ability_modifier(8) == -1
        assert ability_modifier(6) == -2
        assert ability_modifier(1) == -5


class TestProficiencyBonus:
    def test_level_brackets(self):
        assert proficiency_bonus(1) == 2
        assert proficiency_bonus(4) == 2
        assert proficiency_bonus(5) == 3
        assert proficiency_bonus(8) == 3
        assert proficiency_bonus(9) == 4
        assert proficiency_bonus(12) == 4
        assert proficiency_bonus(13) == 5
        assert proficiency_bonus(16) == 5
        assert proficiency_bonus(17) == 6
        assert proficiency_bonus(20) == 6


# ---------------------------------------------------------------------------
# Validate check (LLM output sanitization)
# ---------------------------------------------------------------------------

class TestValidateCheck:
    def test_valid_check(self):
        result = validate_check({"skill": "stealth", "dc": 15, "reason": "sneaking"})
        assert result["skill"] == "stealth"
        assert result["dc"] == 15
        assert result["reason"] == "sneaking"
        assert result["advantage"] is False
        assert result["disadvantage"] is False

    def test_invalid_skill_defaults_to_perception(self):
        result = validate_check({"skill": "lockpicking", "dc": 12})
        assert result["skill"] == "perception"

    def test_empty_skill_defaults_to_perception(self):
        result = validate_check({"skill": "", "dc": 12})
        assert result["skill"] == "perception"

    def test_dc_clamped_low(self):
        result = validate_check({"skill": "stealth", "dc": 2})
        assert result["dc"] == 5

    def test_dc_clamped_high(self):
        result = validate_check({"skill": "stealth", "dc": 50})
        assert result["dc"] == 30

    def test_dc_non_integer_defaults(self):
        result = validate_check({"skill": "stealth", "dc": "hard"})
        assert result["dc"] == 15

    def test_advantage_passed_through(self):
        result = validate_check({"skill": "stealth", "dc": 12, "advantage": True})
        assert result["advantage"] is True
        assert result["disadvantage"] is False

    def test_disadvantage_passed_through(self):
        result = validate_check({"skill": "stealth", "dc": 12, "disadvantage": True})
        assert result["advantage"] is False
        assert result["disadvantage"] is True

    def test_non_bool_advantage_ignored(self):
        result = validate_check({"skill": "stealth", "dc": 12, "advantage": "yes"})
        assert result["advantage"] is False

    def test_all_valid_skills_accepted(self):
        for skill in VALID_SKILLS:
            result = validate_check({"skill": skill, "dc": 10})
            assert result["skill"] == skill

    def test_case_insensitive_skill(self):
        result = validate_check({"skill": "STEALTH", "dc": 10})
        assert result["skill"] == "stealth"

    def test_whitespace_stripped(self):
        result = validate_check({"skill": "  perception  ", "dc": 10})
        assert result["skill"] == "perception"


# ---------------------------------------------------------------------------
# Determine roll mode (advantage/disadvantage resolution)
# ---------------------------------------------------------------------------

class TestDetermineRollMode:
    def test_straight_by_default(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            skill="stealth",
        )
        assert mode == "straight"

    def test_advantage_from_proposal(self):
        mode = determine_roll_mode(
            proposed_advantage=True,
            proposed_disadvantage=False,
            skill="stealth",
        )
        assert mode == "advantage"

    def test_disadvantage_from_proposal(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=True,
            skill="stealth",
        )
        assert mode == "disadvantage"

    def test_advantage_and_disadvantage_cancel(self):
        mode = determine_roll_mode(
            proposed_advantage=True,
            proposed_disadvantage=True,
            skill="stealth",
        )
        assert mode == "straight"

    def test_poisoned_gives_disadvantage(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "spider"}],
            skill="stealth",
        )
        assert mode == "disadvantage"

    def test_poisoned_cancelled_by_advantage(self):
        mode = determine_roll_mode(
            proposed_advantage=True,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "spider"}],
            skill="stealth",
        )
        assert mode == "straight"

    def test_exhaustion_gives_disadvantage(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "exhaustion_1", "source": "travel"}],
            skill="perception",
        )
        assert mode == "disadvantage"

    def test_frightened_gives_disadvantage(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "frightened", "duration_turns": 2, "source": "dragon"}],
            skill="athletics",
        )
        assert mode == "disadvantage"

    def test_restrained_disadvantage_on_affected_skills(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "restrained", "duration_turns": 1, "source": "web"}],
            skill="acrobatics",
        )
        assert mode == "disadvantage"

    def test_restrained_no_disadvantage_on_unaffected_skills(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[{"condition_id": "restrained", "duration_turns": 1, "source": "web"}],
            skill="perception",
        )
        assert mode == "straight"

    def test_darkness_disadvantage_on_perception(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            environmental_effects=["darkness"],
            skill="perception",
        )
        assert mode == "disadvantage"

    def test_darkness_no_effect_on_stealth(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            environmental_effects=["darkness"],
            skill="stealth",
        )
        assert mode == "straight"

    def test_multiple_disadvantage_sources_still_disadvantage(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            conditions=[
                {"condition_id": "poisoned", "duration_turns": 3, "source": "venom"},
                {"condition_id": "exhaustion_1", "source": "fatigue"},
            ],
            skill="stealth",
        )
        assert mode == "disadvantage"

    def test_multiple_disadvantage_cancelled_by_one_advantage(self):
        mode = determine_roll_mode(
            proposed_advantage=True,
            proposed_disadvantage=False,
            conditions=[
                {"condition_id": "poisoned", "duration_turns": 3, "source": "venom"},
                {"condition_id": "exhaustion_1", "source": "fatigue"},
            ],
            skill="stealth",
        )
        assert mode == "straight"


# ---------------------------------------------------------------------------
# Resolve check with advantage/disadvantage
# ---------------------------------------------------------------------------

class TestResolveCheck:
    def _base_check(self, **overrides):
        check = {"skill": "stealth", "dc": 15, "reason": "sneaking", "advantage": False, "disadvantage": False}
        check.update(overrides)
        return check

    def _scores(self):
        return {"dexterity": 16, "wisdom": 14, "strength": 10, "constitution": 14, "intelligence": 12, "charisma": 10}

    def test_straight_roll(self):
        with patch("relay.checks.resolver.random.randint", return_value=12):
            result = resolve_check(self._base_check(), self._scores(), ["stealth"], 6)
        assert result["roll"] == 12
        assert result["dice"] == [12]
        assert result["roll_mode"] == "straight"
        assert result["modifier"] == 3 + 3  # DEX mod + prof bonus
        assert result["total"] == 12 + 6
        assert result["passed"] is True

    def test_advantage_takes_higher(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(advantage=True),
                self._scores(), ["stealth"], 6,
            )
        assert result["roll"] == 18
        assert result["dice"] == [5, 18]
        assert result["roll_mode"] == "advantage"

    def test_disadvantage_takes_lower(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(disadvantage=True),
                self._scores(), ["stealth"], 6,
            )
        assert result["roll"] == 5
        assert result["dice"] == [5, 18]
        assert result["roll_mode"] == "disadvantage"

    def test_condition_disadvantage_applied(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(),
                self._scores(), ["stealth"], 6,
                conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "venom"}],
            )
        assert result["roll_mode"] == "disadvantage"
        assert result["roll"] == 5

    def test_environmental_disadvantage(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(skill="perception"),
                self._scores(), ["perception"], 6,
                environmental_effects=["darkness"],
            )
        assert result["roll_mode"] == "disadvantage"
        assert result["roll"] == 5

    def test_no_proficiency_lower_modifier(self):
        with patch("relay.checks.resolver.random.randint", return_value=12):
            result = resolve_check(self._base_check(), self._scores(), [], 6)
        assert result["modifier"] == 3  # DEX mod only, no prof

    def test_pass_fail_boundary(self):
        with patch("relay.checks.resolver.random.randint", return_value=9):
            result = resolve_check(
                self._base_check(dc=15),
                self._scores(), ["stealth"], 6,
            )
        # roll 9 + mod 6 = 15, DC 15 -> pass (meets or exceeds)
        assert result["total"] == 15
        assert result["passed"] is True

        with patch("relay.checks.resolver.random.randint", return_value=8):
            result = resolve_check(
                self._base_check(dc=15),
                self._scores(), ["stealth"], 6,
            )
        assert result["total"] == 14
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Passive checks
# ---------------------------------------------------------------------------

class TestPassiveChecks:
    def _scores(self):
        return {"dexterity": 16, "wisdom": 14, "strength": 10, "constitution": 14, "intelligence": 12, "charisma": 10}

    def test_compute_passive_perception(self):
        # WIS 14 = +2 mod, level 6 prof bonus = +3, proficient -> 10 + 2 + 3 = 15
        val = compute_passive_check("perception", self._scores(), ["perception"], 6)
        assert val == 15

    def test_compute_passive_without_proficiency(self):
        # WIS 14 = +2 mod, not proficient -> 10 + 2 = 12
        val = compute_passive_check("perception", self._scores(), [], 6)
        assert val == 12

    def test_compute_passive_investigation(self):
        # INT 12 = +1 mod, level 6 prof bonus = +3, proficient -> 10 + 1 + 3 = 14
        val = compute_passive_check("investigation", self._scores(), ["investigation"], 6)
        assert val == 14

    def test_poisoned_reduces_passive_by_5(self):
        val = compute_passive_check(
            "perception", self._scores(), ["perception"], 6,
            conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "spider"}],
        )
        assert val == 10  # 15 - 5

    def test_exhaustion_reduces_passive_by_5(self):
        val = compute_passive_check(
            "perception", self._scores(), ["perception"], 6,
            conditions=[{"condition_id": "exhaustion_1", "source": "travel"}],
        )
        assert val == 10

    def test_evaluate_passive_detects_hidden_element(self):
        scene_state = {
            "hidden_elements": [
                {
                    "id": "secret_door_1",
                    "dc": 14,
                    "skill": "perception",
                    "hint": "You notice a faint draft coming from behind the bookshelf.",
                },
            ],
        }
        results = evaluate_passive_checks(
            self._scores(), ["perception"], 6, scene_state,
        )
        assert len(results) == 1
        assert results[0]["element_id"] == "secret_door_1"
        assert results[0]["passive_value"] == 15
        assert results[0]["hint"] == "You notice a faint draft coming from behind the bookshelf."

    def test_evaluate_passive_misses_high_dc(self):
        scene_state = {
            "hidden_elements": [
                {"id": "trap_1", "dc": 20, "skill": "perception", "hint": "trap hint"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(), ["perception"], 6, scene_state,
        )
        assert len(results) == 0

    def test_evaluate_passive_no_hidden_elements(self):
        results = evaluate_passive_checks(self._scores(), ["perception"], 6, {})
        assert len(results) == 0

    def test_evaluate_passive_multiple_elements(self):
        scene_state = {
            "hidden_elements": [
                {"id": "trap_1", "dc": 14, "skill": "perception", "hint": "trap"},
                {"id": "secret_2", "dc": 20, "skill": "perception", "hint": "secret"},
                {"id": "clue_3", "dc": 12, "skill": "investigation", "hint": "clue"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(), ["perception", "investigation"], 6, scene_state,
        )
        # passive perception = 15 (beats 14, not 20)
        # passive investigation = 14 (beats 12)
        assert len(results) == 2
        ids = {r["element_id"] for r in results}
        assert "trap_1" in ids
        assert "clue_3" in ids

    def test_evaluate_passive_invalid_skill_defaults(self):
        scene_state = {
            "hidden_elements": [
                {"id": "x", "dc": 10, "skill": "lockpicking", "hint": "hint"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(), ["perception"], 6, scene_state,
        )
        # Invalid skill defaults to perception, passive perception = 15 >= 10
        assert len(results) == 1
