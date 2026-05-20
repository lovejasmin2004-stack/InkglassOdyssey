"""Tests for the check system: validation, advantage/disadvantage, passive checks.

Step 10 additions:
 - (#1)  sleight_of_hand and animal_handling skill tests
 - (#4)  Shared roll_d20 tests
 - (#5)  Stunned/incapacitated auto-fail tests
 - (#6)  Contested check tests
 - (#7)  Passive check already-triggered filtering tests
 - (#8)  Charmed advantage on social checks tests
 - (#9)  Natural 1/20 tracking tests
 - (#11) Batch validation cap tests
"""

from __future__ import annotations

from unittest.mock import patch

from relay.checks.resolver import (
    MAX_CHECKS_PER_TURN,
    SOCIAL_SKILLS,
    VALID_SKILLS,
    ability_modifier,
    compute_passive_check,
    determine_roll_mode,
    evaluate_passive_checks,
    is_incapable_of_checks,
    proficiency_bonus,
    resolve_check,
    resolve_contested_check,
    roll_d20,
    validate_check,
    validate_checks_batch,
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
        """Exhaustion level 1+ imposes disadvantage on ability checks."""
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            skill="perception",
            exhaustion_level=1,
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
                self._scores(),
                ["stealth"],
                6,
            )
        assert result["roll"] == 18
        assert result["dice"] == [5, 18]
        assert result["roll_mode"] == "advantage"

    def test_disadvantage_takes_lower(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(disadvantage=True),
                self._scores(),
                ["stealth"],
                6,
            )
        assert result["roll"] == 5
        assert result["dice"] == [5, 18]
        assert result["roll_mode"] == "disadvantage"

    def test_condition_disadvantage_applied(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(),
                self._scores(),
                ["stealth"],
                6,
                conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "venom"}],
            )
        assert result["roll_mode"] == "disadvantage"
        assert result["roll"] == 5

    def test_environmental_disadvantage(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            result = resolve_check(
                self._base_check(skill="perception"),
                self._scores(),
                ["perception"],
                6,
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
                self._scores(),
                ["stealth"],
                6,
            )
        # roll 9 + mod 6 = 15, DC 15 -> pass (meets or exceeds)
        assert result["total"] == 15
        assert result["passed"] is True

        with patch("relay.checks.resolver.random.randint", return_value=8):
            result = resolve_check(
                self._base_check(dc=15),
                self._scores(),
                ["stealth"],
                6,
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
            "perception",
            self._scores(),
            ["perception"],
            6,
            conditions=[{"condition_id": "poisoned", "duration_turns": 3, "source": "spider"}],
        )
        assert val == 10  # 15 - 5

    def test_exhaustion_reduces_passive_by_5(self):
        val = compute_passive_check(
            "perception",
            self._scores(),
            ["perception"],
            6,
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
            self._scores(),
            ["perception"],
            6,
            scene_state,
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
            self._scores(),
            ["perception"],
            6,
            scene_state,
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
            self._scores(),
            ["perception", "investigation"],
            6,
            scene_state,
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
            self._scores(),
            ["perception"],
            6,
            scene_state,
        )
        # Invalid skill defaults to perception, passive perception = 15 >= 10
        assert len(results) == 1


# ---------------------------------------------------------------------------
# (#1) New skills: sleight_of_hand and animal_handling
# ---------------------------------------------------------------------------


class TestNewSkills:
    def test_sleight_of_hand_in_valid_skills(self):
        assert "sleight_of_hand" in VALID_SKILLS

    def test_animal_handling_in_valid_skills(self):
        assert "animal_handling" in VALID_SKILLS

    def test_sleight_of_hand_uses_dexterity(self):
        result = validate_check({"skill": "sleight_of_hand", "dc": 12})
        assert result["skill"] == "sleight_of_hand"
        # Resolve: DEX 16 = +3 mod, proficient, level 6 prof +3 = total mod 6
        with patch("relay.checks.resolver.random.randint", return_value=10):
            check = resolve_check(
                result,
                {"dexterity": 16, "wisdom": 10, "strength": 10, "constitution": 10, "intelligence": 10, "charisma": 10},
                ["sleight_of_hand"],
                6,
            )
        assert check["modifier"] == 6  # +3 DEX + +3 prof

    def test_animal_handling_uses_wisdom(self):
        result = validate_check({"skill": "animal_handling", "dc": 10})
        assert result["skill"] == "animal_handling"
        with patch("relay.checks.resolver.random.randint", return_value=10):
            check = resolve_check(
                result,
                {"dexterity": 10, "wisdom": 16, "strength": 10, "constitution": 10, "intelligence": 10, "charisma": 10},
                ["animal_handling"],
                6,
            )
        assert check["modifier"] == 6  # +3 WIS + +3 prof


# ---------------------------------------------------------------------------
# (#4) Shared roll_d20
# ---------------------------------------------------------------------------


class TestRollD20:
    def test_straight_roll(self):
        with patch("relay.checks.resolver.random.randint", return_value=15):
            roll, dice = roll_d20("straight")
        assert roll == 15
        assert dice == [15]

    def test_advantage_takes_higher(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            roll, dice = roll_d20("advantage")
        assert roll == 18
        assert dice == [5, 18]

    def test_disadvantage_takes_lower(self):
        with patch("relay.checks.resolver.random.randint", side_effect=[5, 18]):
            roll, dice = roll_d20("disadvantage")
        assert roll == 5
        assert dice == [5, 18]


# ---------------------------------------------------------------------------
# (#5) Stunned / incapacitated auto-fail
# ---------------------------------------------------------------------------


class TestStunnedAutoFail:
    def _scores(self):
        return {"dexterity": 16, "wisdom": 14, "strength": 10, "constitution": 14, "intelligence": 12, "charisma": 10}

    def test_is_incapable_stunned(self):
        assert is_incapable_of_checks([{"condition_id": "stunned", "source": "spell"}])

    def test_is_incapable_incapacitated(self):
        assert is_incapable_of_checks([{"condition_id": "incapacitated", "source": "effect"}])

    def test_is_not_incapable_poisoned(self):
        assert not is_incapable_of_checks([{"condition_id": "poisoned", "source": "venom"}])

    def test_is_not_incapable_empty(self):
        assert not is_incapable_of_checks([])
        assert not is_incapable_of_checks(None)

    def test_resolve_check_auto_fails_when_stunned(self):
        check = {"skill": "stealth", "dc": 5, "reason": "sneaking", "advantage": False, "disadvantage": False}
        result = resolve_check(
            check,
            self._scores(),
            ["stealth"],
            6,
            conditions=[{"condition_id": "stunned", "source": "spell"}],
        )
        assert result["passed"] is False
        assert result["roll_mode"] == "auto_fail"
        assert result["auto_fail_reason"] == "incapacitated"
        assert result["total"] == 0

    def test_resolve_check_auto_fails_when_incapacitated(self):
        check = {"skill": "perception", "dc": 5, "reason": "looking", "advantage": False, "disadvantage": False}
        result = resolve_check(
            check,
            self._scores(),
            ["perception"],
            6,
            conditions=[{"condition_id": "incapacitated", "source": "effect"}],
        )
        assert result["passed"] is False
        assert result["roll_mode"] == "auto_fail"


# ---------------------------------------------------------------------------
# (#6) Contested checks
# ---------------------------------------------------------------------------


class TestContestedChecks:
    def _scores_strong(self):
        return {"strength": 18, "dexterity": 10, "constitution": 14, "intelligence": 10, "wisdom": 10, "charisma": 10}

    def _scores_dex(self):
        return {"strength": 10, "dexterity": 18, "constitution": 14, "intelligence": 10, "wisdom": 10, "charisma": 10}

    def test_attacker_wins(self):
        """Higher total wins the contest."""
        att_check = {"skill": "athletics", "dc": 0, "reason": "grapple", "advantage": False, "disadvantage": False}
        def_check = {"skill": "acrobatics", "dc": 0, "reason": "grapple", "advantage": False, "disadvantage": False}

        with patch("relay.checks.resolver.random.randint", side_effect=[15, 8]):
            result = resolve_contested_check(
                att_check,
                self._scores_strong(),
                ["athletics"],
                6,
                def_check,
                self._scores_dex(),
                ["acrobatics"],
                6,
            )
        assert result["winner"] == "attacker"
        assert result["attacker_total"] > result["defender_total"]

    def test_defender_wins_on_tie(self):
        """Ties go to the defender (status quo holds)."""
        att_check = {"skill": "athletics", "dc": 0, "reason": "grapple", "advantage": False, "disadvantage": False}
        def_check = {"skill": "athletics", "dc": 0, "reason": "grapple", "advantage": False, "disadvantage": False}

        # Same scores, same proficiency, same roll = tie → defender wins
        with patch("relay.checks.resolver.random.randint", return_value=10):
            result = resolve_contested_check(
                att_check,
                self._scores_strong(),
                ["athletics"],
                6,
                def_check,
                self._scores_strong(),
                ["athletics"],
                6,
            )
        assert result["winner"] == "defender"
        assert result["tie"] is True

    def test_contest_returns_both_results(self):
        att_check = {"skill": "deception", "dc": 0, "reason": "lying", "advantage": False, "disadvantage": False}
        def_check = {"skill": "insight", "dc": 0, "reason": "lying", "advantage": False, "disadvantage": False}

        with patch("relay.checks.resolver.random.randint", side_effect=[12, 14]):
            result = resolve_contested_check(
                att_check,
                self._scores_dex(),
                ["deception"],
                5,
                def_check,
                self._scores_dex(),
                ["insight"],
                5,
            )
        assert "attacker" in result
        assert "defender" in result
        assert result["attacker"]["skill"] == "deception"
        assert result["defender"]["skill"] == "insight"


# ---------------------------------------------------------------------------
# (#7) Passive check already-triggered filtering
# ---------------------------------------------------------------------------


class TestPassiveAlreadyTriggered:
    def _scores(self):
        return {"dexterity": 16, "wisdom": 14, "strength": 10, "constitution": 14, "intelligence": 12, "charisma": 10}

    def test_already_triggered_skipped(self):
        scene_state = {
            "hidden_elements": [
                {"id": "trap_1", "dc": 10, "skill": "perception", "hint": "trap"},
                {"id": "secret_2", "dc": 10, "skill": "perception", "hint": "secret"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(),
            ["perception"],
            6,
            scene_state,
            already_triggered=["trap_1"],
        )
        assert len(results) == 1
        assert results[0]["element_id"] == "secret_2"

    def test_all_triggered_returns_empty(self):
        scene_state = {
            "hidden_elements": [
                {"id": "trap_1", "dc": 10, "skill": "perception", "hint": "trap"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(),
            ["perception"],
            6,
            scene_state,
            already_triggered=["trap_1"],
        )
        assert len(results) == 0

    def test_none_triggered_returns_all(self):
        scene_state = {
            "hidden_elements": [
                {"id": "trap_1", "dc": 10, "skill": "perception", "hint": "trap"},
                {"id": "secret_2", "dc": 10, "skill": "perception", "hint": "secret"},
            ],
        }
        results = evaluate_passive_checks(
            self._scores(),
            ["perception"],
            6,
            scene_state,
            already_triggered=None,
        )
        assert len(results) == 2


# ---------------------------------------------------------------------------
# (#8) Charmed advantage on social checks
# ---------------------------------------------------------------------------


class TestCharmedAdvantage:
    def test_charmer_gets_advantage_on_persuasion(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            skill="persuasion",
            charmer_checking=True,
        )
        assert mode == "advantage"

    def test_charmer_gets_advantage_on_deception(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            skill="deception",
            charmer_checking=True,
        )
        assert mode == "advantage"

    def test_charmer_no_advantage_on_stealth(self):
        """charmer_checking only grants advantage on SOCIAL_SKILLS."""
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=False,
            skill="stealth",
            charmer_checking=True,
        )
        assert mode == "straight"

    def test_charmer_advantage_cancelled_by_disadvantage(self):
        mode = determine_roll_mode(
            proposed_advantage=False,
            proposed_disadvantage=True,
            skill="persuasion",
            charmer_checking=True,
        )
        assert mode == "straight"

    def test_social_skills_set(self):
        assert "persuasion" in SOCIAL_SKILLS
        assert "deception" in SOCIAL_SKILLS
        assert "intimidation" in SOCIAL_SKILLS
        assert "performance" in SOCIAL_SKILLS
        assert "stealth" not in SOCIAL_SKILLS


# ---------------------------------------------------------------------------
# (#9) Natural 1 / 20 tracking
# ---------------------------------------------------------------------------


class TestNatural20And1:
    def _scores(self):
        return {"dexterity": 16, "wisdom": 14, "strength": 10, "constitution": 14, "intelligence": 12, "charisma": 10}

    def test_natural_20_flagged(self):
        check = {"skill": "stealth", "dc": 25, "reason": "test", "advantage": False, "disadvantage": False}
        with patch("relay.checks.resolver.random.randint", return_value=20):
            result = resolve_check(check, self._scores(), ["stealth"], 6)
        assert result["natural_20"] is True
        assert result["natural_1"] is False

    def test_natural_1_flagged(self):
        check = {"skill": "stealth", "dc": 5, "reason": "test", "advantage": False, "disadvantage": False}
        with patch("relay.checks.resolver.random.randint", return_value=1):
            result = resolve_check(check, self._scores(), ["stealth"], 6)
        assert result["natural_1"] is True
        assert result["natural_20"] is False

    def test_normal_roll_no_flags(self):
        check = {"skill": "stealth", "dc": 15, "reason": "test", "advantage": False, "disadvantage": False}
        with patch("relay.checks.resolver.random.randint", return_value=10):
            result = resolve_check(check, self._scores(), ["stealth"], 6)
        assert result["natural_1"] is False
        assert result["natural_20"] is False

    def test_auto_fail_no_natural_flags(self):
        """Stunned auto-fail should not report natural 1/20."""
        check = {"skill": "stealth", "dc": 5, "reason": "test", "advantage": False, "disadvantage": False}
        result = resolve_check(
            check,
            self._scores(),
            ["stealth"],
            6,
            conditions=[{"condition_id": "stunned", "source": "spell"}],
        )
        assert result["natural_1"] is False
        assert result["natural_20"] is False


# ---------------------------------------------------------------------------
# (#11) Batch validation cap
# ---------------------------------------------------------------------------


class TestBatchValidation:
    def test_within_limit(self):
        checks = [{"skill": "stealth", "dc": 12}] * MAX_CHECKS_PER_TURN
        result = validate_checks_batch(checks)
        assert len(result) == MAX_CHECKS_PER_TURN

    def test_exceeds_limit_capped(self):
        checks = [{"skill": "stealth", "dc": 12}] * (MAX_CHECKS_PER_TURN + 5)
        result = validate_checks_batch(checks)
        assert len(result) == MAX_CHECKS_PER_TURN

    def test_empty_list(self):
        result = validate_checks_batch([])
        assert len(result) == 0

    def test_single_check(self):
        result = validate_checks_batch([{"skill": "perception", "dc": 15}])
        assert len(result) == 1
        assert result[0]["skill"] == "perception"

    def test_validates_each_check(self):
        """Each check in the batch gets validated (invalid skill → perception)."""
        checks = [
            {"skill": "lockpicking", "dc": 15},
            {"skill": "stealth", "dc": 12},
        ]
        result = validate_checks_batch(checks)
        assert result[0]["skill"] == "perception"  # invalid → default
        assert result[1]["skill"] == "stealth"  # valid
