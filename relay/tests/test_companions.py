"""Tests for the companion system.

Covers: recruitment validation, combat AI, loyalty strain, incapacitation,
ambient behaviour, dismissal, and endpoint integration.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token
from relay.companions.ambient import (
    compute_mood_modifier,
    find_world_event_reaction,
    should_trigger_comment,
)
from relay.companions.combat_ai import apply_directive, resolve_companion_action
from relay.companions.loyalty import (
    apply_dismissal,
    check_confrontation_threshold,
    clear_exhaustion_on_rest,
    handle_incapacitation,
    recover_after_combat,
)
from relay.companions.manager import (
    AffectionTooLowError,
    AlreadyRecruitedError,
    CompanionLimitError,
    ConditionNotMetError,
    add_companion,
    create_companion_entry,
    find_companion,
    remove_companion,
    validate_recruitment,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

COMPANION_DATA = {
    "recruitment": {
        "affection_threshold": 50,
        "recruitment_scenario_id": "recruit_kaelen",
        "recruitment_conditions": ["quest_complete_forest"],
    },
    "combat_profile": {
        "behavior_type": "aggressive",
        "abilities": ["slash", "cleave"],
        "directive_vocabulary": {
            "stay back": "defensive",
            "focus the caster": "aggressive",
            "heal me": "supportive",
        },
    },
    "ambient_behavior": {
        "comment_frequency": "probability_0.5",
        "trigger_categories": ["new_region", "combat_start"],
        "mood_modifier": 0.1,
    },
    "loyalty_strain_threshold": 3,
    "dismissal_relationship_modifier": -10,
    "farewell_template": "farewell_kaelen",
    "reunion_template": "reunion_kaelen",
    "world_event_reactions": [
        {"event_id": "dragon_attack", "comment": "We should flee!", "relationship_modifier": -2},
    ],
    "level": 5,
    "ability_scores": {"strength": 16, "dexterity": 12, "constitution": 14, "wisdom": 10},
}


# ===========================================================================
# Unit tests — manager.py
# ===========================================================================


class TestValidateRecruitment:
    def test_successful_recruitment(self):
        validate_recruitment(
            npc_id="kaelen",
            companion_data=COMPANION_DATA,
            relationship_score=60,
            current_companions=[],
            world_flags={"quest_complete_forest": True},
        )

    def test_affection_too_low(self):
        with pytest.raises(AffectionTooLowError):
            validate_recruitment(
                npc_id="kaelen",
                companion_data=COMPANION_DATA,
                relationship_score=30,
                current_companions=[],
                world_flags={"quest_complete_forest": True},
            )

    def test_affection_at_threshold(self):
        validate_recruitment(
            npc_id="kaelen",
            companion_data=COMPANION_DATA,
            relationship_score=50,
            current_companions=[],
            world_flags={"quest_complete_forest": True},
        )

    def test_companion_limit_reached(self):
        existing = [{"npc_id": "other_npc", "active": True}]
        with pytest.raises(CompanionLimitError):
            validate_recruitment(
                npc_id="kaelen",
                companion_data=COMPANION_DATA,
                relationship_score=60,
                current_companions=existing,
                max_active_companions=1,
                world_flags={"quest_complete_forest": True},
            )

    def test_companion_limit_allows_when_inactive(self):
        existing = [{"npc_id": "other_npc", "active": False}]
        validate_recruitment(
            npc_id="kaelen",
            companion_data=COMPANION_DATA,
            relationship_score=60,
            current_companions=existing,
            max_active_companions=1,
            world_flags={"quest_complete_forest": True},
        )

    def test_already_recruited(self):
        existing = [{"npc_id": "kaelen", "active": True}]
        with pytest.raises(AlreadyRecruitedError):
            validate_recruitment(
                npc_id="kaelen",
                companion_data=COMPANION_DATA,
                relationship_score=60,
                current_companions=existing,
                world_flags={"quest_complete_forest": True},
            )

    def test_condition_not_met(self):
        with pytest.raises(ConditionNotMetError):
            validate_recruitment(
                npc_id="kaelen",
                companion_data=COMPANION_DATA,
                relationship_score=60,
                current_companions=[],
                world_flags={},
            )

    def test_no_conditions_required(self):
        data = {**COMPANION_DATA, "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"}}
        validate_recruitment(
            npc_id="kaelen",
            companion_data=data,
            relationship_score=20,
            current_companions=[],
        )


class TestCreateCompanionEntry:
    def test_entry_structure(self):
        entry = create_companion_entry(
            npc_id="kaelen",
            companion_data=COMPANION_DATA,
            npc_hp_max=45,
        )
        assert entry["npc_id"] == "kaelen"
        assert entry["hp_current"] == 45
        assert entry["hp_max"] == 45
        assert entry["conditions"] == []
        assert entry["exhaustion_level"] == 0
        assert entry["loyalty_strain"] == 0
        assert entry["behavior_type"] == "aggressive"
        assert entry["active"] is True


class TestCompanionList:
    def test_add_companion(self):
        companions = []
        entry = {"npc_id": "kaelen", "active": True}
        result = add_companion(companions, entry)
        assert len(result) == 1
        assert result[0]["npc_id"] == "kaelen"

    def test_remove_companion(self):
        companions = [{"npc_id": "kaelen"}, {"npc_id": "other"}]
        result = remove_companion(companions, "kaelen")
        assert len(result) == 1
        assert result[0]["npc_id"] == "other"

    def test_find_companion(self):
        companions = [{"npc_id": "kaelen"}, {"npc_id": "other"}]
        found = find_companion(companions, "kaelen")
        assert found is not None
        assert found["npc_id"] == "kaelen"

    def test_find_companion_not_found(self):
        companions = [{"npc_id": "other"}]
        assert find_companion(companions, "kaelen") is None


# ===========================================================================
# Unit tests — combat_ai.py
# ===========================================================================


class TestCombatAI:
    def test_aggressive_attack_hit(self):
        companion = {"npc_id": "kaelen", "hp_current": 30, "behavior_type": "aggressive", "active": True}
        target = {"id": "goblin_1", "ac": 10, "hp_current": 15, "ability_scores": {}, "level": 1}

        with patch("random.randint", side_effect=[18, 4]):
            action = resolve_companion_action(
                companion=companion,
                companion_data=COMPANION_DATA,
                target=target,
            )
        assert action["action_type"] == "attack"
        assert action["hit"] is True
        assert action["damage"] > 0
        assert action["target_id"] == "goblin_1"

    def test_aggressive_attack_miss(self):
        companion = {"npc_id": "kaelen", "hp_current": 30, "behavior_type": "aggressive", "active": True}
        target = {"id": "goblin_1", "ac": 25, "hp_current": 15, "ability_scores": {}, "level": 1}

        with patch("random.randint", side_effect=[2]):
            action = resolve_companion_action(
                companion=companion,
                companion_data=COMPANION_DATA,
                target=target,
            )
        assert action["action_type"] == "attack"
        assert action["hit"] is False
        assert action["damage"] == 0

    def test_aggressive_natural_1_misses(self):
        companion = {"npc_id": "kaelen", "hp_current": 30, "behavior_type": "aggressive", "active": True}
        target = {"id": "goblin_1", "ac": 5, "hp_current": 15, "ability_scores": {}, "level": 1}

        with patch("random.randint", side_effect=[1]):
            action = resolve_companion_action(
                companion=companion,
                companion_data=COMPANION_DATA,
                target=target,
            )
        assert action["hit"] is False

    def test_aggressive_natural_20_crits(self):
        companion = {"npc_id": "kaelen", "hp_current": 30, "behavior_type": "aggressive", "active": True}
        target = {"id": "goblin_1", "ac": 30, "hp_current": 15, "ability_scores": {}, "level": 1}

        with patch("random.randint", side_effect=[20, 3, 5]):
            action = resolve_companion_action(
                companion=companion,
                companion_data=COMPANION_DATA,
                target=target,
            )
        assert action["hit"] is True
        assert action["critical"] is True

    def test_aggressive_no_target(self):
        companion = {"npc_id": "kaelen", "hp_current": 30, "behavior_type": "aggressive", "active": True}
        action = resolve_companion_action(
            companion=companion,
            companion_data=COMPANION_DATA,
            target=None,
        )
        assert action["action_type"] == "none"
        assert action["reason"] == "no_target"

    def test_supportive_heals_lowest_hp(self):
        companion = {"npc_id": "healer", "hp_current": 30, "behavior_type": "supportive", "active": True}
        data = {**COMPANION_DATA, "combat_profile": {"behavior_type": "supportive", "abilities": ["heal", "bless"]}}
        data["ability_scores"] = {"wisdom": 16, "strength": 10}
        allies = [
            {"id": "player_1", "hp_current": 20, "hp_max": 40},
            {"id": "player_2", "hp_current": 5, "hp_max": 30},
        ]

        with patch("random.randint", side_effect=[6]):
            action = resolve_companion_action(
                companion=companion,
                companion_data=data,
                allies=allies,
            )
        assert action["action_type"] == "heal"
        assert action["target_id"] == "player_2"
        assert action["healing"] >= 1

    def test_supportive_no_allies(self):
        companion = {"npc_id": "healer", "hp_current": 30, "behavior_type": "supportive", "active": True}
        action = resolve_companion_action(
            companion=companion,
            companion_data=COMPANION_DATA,
            allies=None,
        )
        assert action["action_type"] == "none"
        assert action["reason"] == "no_allies"

    def test_defensive_protect(self):
        companion = {"npc_id": "tank", "hp_current": 50, "behavior_type": "defensive", "active": True}
        action = resolve_companion_action(
            companion=companion,
            companion_data=COMPANION_DATA,
        )
        assert action["action_type"] == "protect"
        assert action["companion_id"] == "tank"

    def test_incapacitated_companion_no_action(self):
        companion = {"npc_id": "kaelen", "hp_current": 0, "behavior_type": "aggressive", "active": False}
        action = resolve_companion_action(
            companion=companion,
            companion_data=COMPANION_DATA,
        )
        assert action["action_type"] == "none"
        assert action["reason"] == "companion_incapacitated"


class TestDirective:
    def test_apply_known_directive(self):
        companion = {"npc_id": "kaelen", "behavior_type": "aggressive"}
        vocab = COMPANION_DATA["combat_profile"]["directive_vocabulary"]
        result = apply_directive(companion, "stay back", vocab)
        assert result["behavior_type"] == "defensive"

    def test_apply_heal_directive(self):
        companion = {"npc_id": "kaelen", "behavior_type": "aggressive"}
        vocab = COMPANION_DATA["combat_profile"]["directive_vocabulary"]
        result = apply_directive(companion, "heal me", vocab)
        assert result["behavior_type"] == "supportive"

    def test_unknown_directive_no_change(self):
        companion = {"npc_id": "kaelen", "behavior_type": "aggressive"}
        result = apply_directive(companion, "do a backflip", {})
        assert result["behavior_type"] == "aggressive"


# ===========================================================================
# Unit tests — loyalty.py
# ===========================================================================


class TestIncapacitation:
    def test_basic_incapacitation(self):
        companion = {"npc_id": "kaelen", "hp_current": 0, "exhaustion_level": 0, "loyalty_strain": 0, "active": True}
        relationships = {"kaelen": 60}

        result = handle_incapacitation(
            companion=companion,
            companion_data=COMPANION_DATA,
            relationships=relationships,
        )

        assert companion["exhaustion_level"] == 1
        assert companion["loyalty_strain"] == 1
        assert companion["active"] is False
        assert companion["hp_current"] == 0
        assert relationships["kaelen"] == 50  # 60 + (-10)
        assert result["confrontation_triggered"] is False

    def test_incapacitation_triggers_confrontation(self):
        companion = {"npc_id": "kaelen", "hp_current": 0, "exhaustion_level": 1, "loyalty_strain": 2, "active": True}
        relationships = {"kaelen": 40}

        result = handle_incapacitation(
            companion=companion,
            companion_data=COMPANION_DATA,
            relationships=relationships,
        )

        assert companion["loyalty_strain"] == 3
        assert result["confrontation_triggered"] is True

    def test_exhaustion_capped_at_6(self):
        companion = {"npc_id": "kaelen", "hp_current": 0, "exhaustion_level": 6, "loyalty_strain": 0, "active": True}
        relationships = {"kaelen": 60}

        handle_incapacitation(
            companion=companion,
            companion_data=COMPANION_DATA,
            relationships=relationships,
        )
        assert companion["exhaustion_level"] == 6

    def test_relationship_decreases(self):
        companion = {"npc_id": "kaelen", "hp_current": 0, "exhaustion_level": 0, "loyalty_strain": 0, "active": True}
        relationships = {"kaelen": 10}

        handle_incapacitation(
            companion=companion,
            companion_data=COMPANION_DATA,
            relationships=relationships,
        )
        assert relationships["kaelen"] == 0  # 10 + (-10)


class TestConfrontation:
    def test_below_threshold(self):
        companion = {"loyalty_strain": 2}
        assert check_confrontation_threshold(companion, COMPANION_DATA) is False

    def test_at_threshold(self):
        companion = {"loyalty_strain": 3}
        assert check_confrontation_threshold(companion, COMPANION_DATA) is True

    def test_above_threshold(self):
        companion = {"loyalty_strain": 5}
        assert check_confrontation_threshold(companion, COMPANION_DATA) is True


class TestRecovery:
    def test_recover_after_combat(self):
        companion = {
            "npc_id": "kaelen",
            "hp_current": 0,
            "hp_max": 45,
            "active": False,
            "conditions": [{"condition_id": "stunned"}],
        }
        recover_after_combat(companion)
        assert companion["hp_current"] == 45
        assert companion["active"] is True
        assert companion["conditions"] == []

    def test_clear_exhaustion_on_rest(self):
        companion = {"exhaustion_level": 2}
        clear_exhaustion_on_rest(companion)
        assert companion["exhaustion_level"] == 1

    def test_clear_exhaustion_at_zero(self):
        companion = {"exhaustion_level": 0}
        clear_exhaustion_on_rest(companion)
        assert companion["exhaustion_level"] == 0


class TestDismissal:
    def test_basic_dismissal(self):
        companion = {"npc_id": "kaelen"}
        relationships = {"kaelen": 60}

        result = apply_dismissal(
            companion=companion,
            companion_data=COMPANION_DATA,
            relationships=relationships,
        )

        assert relationships["kaelen"] == 50
        assert result["farewell_template"] == "farewell_kaelen"
        assert result["relationship_old"] == 60
        assert result["relationship_new"] == 50


# ===========================================================================
# Unit tests — ambient.py
# ===========================================================================


class TestAmbientTrigger:
    def test_matching_trigger_with_probability(self):
        with patch("random.random", return_value=0.1):
            assert (
                should_trigger_comment(
                    trigger="new_region",
                    companion_data=COMPANION_DATA,
                    turn_number=1,
                )
                is True
            )

    def test_matching_trigger_probability_fail(self):
        with patch("random.random", return_value=0.9):
            assert (
                should_trigger_comment(
                    trigger="new_region",
                    companion_data=COMPANION_DATA,
                    turn_number=1,
                )
                is False
            )

    def test_non_matching_trigger(self):
        assert (
            should_trigger_comment(
                trigger="player_idle",
                companion_data=COMPANION_DATA,
                turn_number=1,
            )
            is False
        )

    def test_every_n_frequency(self):
        data = {
            **COMPANION_DATA,
            "ambient_behavior": {
                "comment_frequency": "every_3",
                "trigger_categories": ["new_region"],
            },
        }
        assert should_trigger_comment(trigger="new_region", companion_data=data, turn_number=3) is True
        assert should_trigger_comment(trigger="new_region", companion_data=data, turn_number=4) is False
        assert should_trigger_comment(trigger="new_region", companion_data=data, turn_number=6) is True


class TestMoodModifier:
    def test_positive_mood(self):
        mood = compute_mood_modifier(
            companion_data=COMPANION_DATA,
            relationship_score=80,
            loyalty_strain=0,
        )
        # 0.1 (base) + 0.8 (relationship) + 0 (strain) = 0.9
        assert mood == pytest.approx(0.9, abs=0.01)

    def test_negative_mood_from_strain(self):
        mood = compute_mood_modifier(
            companion_data=COMPANION_DATA,
            relationship_score=20,
            loyalty_strain=3,
        )
        # 0.1 + 0.2 + (-0.3) = 0.0
        assert mood == pytest.approx(0.0, abs=0.01)

    def test_clamped_at_max(self):
        mood = compute_mood_modifier(
            companion_data=COMPANION_DATA,
            relationship_score=100,
            loyalty_strain=0,
        )
        # 0.1 + 1.0 + 0 = 1.1 → clamped to 1.0
        assert mood == 1.0

    def test_clamped_at_min(self):
        mood = compute_mood_modifier(
            companion_data=COMPANION_DATA,
            relationship_score=-100,
            loyalty_strain=5,
        )
        # 0.1 + (-1.0) + (-0.5) = -1.4 → clamped to -1.0
        assert mood == -1.0


class TestWorldEventReaction:
    def test_find_matching_reaction(self):
        reaction = find_world_event_reaction(COMPANION_DATA, "dragon_attack")
        assert reaction is not None
        assert reaction["comment"] == "We should flee!"

    def test_no_matching_reaction(self):
        assert find_world_event_reaction(COMPANION_DATA, "unknown_event") is None


# ===========================================================================
# Integration tests — endpoints
# ===========================================================================


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
def character_id(db_client, auth_header, session_header):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Companion Tester",
            "specialisation_path_id": "warrior",
            "ability_scores": {
                "strength": 16,
                "dexterity": 12,
                "constitution": 14,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 10,
            },
            "skill_proficiencies": ["athletics", "intimidation"],
            "saving_throw_proficiencies": ["strength", "constitution"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    cid = resp.json()["id"]

    db_client.patch(
        f"/character/{cid}",
        json={"relationships": {"kaelen": 60}},
        headers=session_header,
    )
    return cid


class TestRecruitEndpoint:
    def test_recruit_companion(self, db_client, session_header, character_id):
        resp = db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": COMPANION_DATA,
                "npc_hp_max": 45,
                "world_flags": {"quest_complete_forest": True},
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["recruited"] is True
        assert data["npc_id"] == "kaelen"
        assert data["companion_state"]["hp_current"] == 45
        assert data["companion_state"]["behavior_type"] == "aggressive"

    def test_recruit_affection_too_low(self, db_client, session_header, character_id):
        db_client.patch(
            f"/character/{character_id}",
            json={"relationships": {"kaelen": 10}},
            headers=session_header,
        )
        resp = db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": COMPANION_DATA,
                "npc_hp_max": 45,
                "world_flags": {"quest_complete_forest": True},
            },
            headers=session_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "affection_too_low"

    def test_recruit_duplicate(self, db_client, session_header, character_id):
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": COMPANION_DATA,
                "npc_hp_max": 45,
                "world_flags": {"quest_complete_forest": True},
            },
            headers=session_header,
        )
        resp = db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": COMPANION_DATA,
                "npc_hp_max": 45,
                "world_flags": {"quest_complete_forest": True},
            },
            headers=session_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "already_recruited"

    def test_recruit_limit_reached(self, db_client, session_header, character_id):
        db_client.patch(
            f"/character/{character_id}",
            json={"relationships": {"kaelen": 60, "other_npc": 60}},
            headers=session_header,
        )
        data_no_conditions = {
            **COMPANION_DATA,
            "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"},
        }
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": data_no_conditions,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )
        resp = db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "other_npc",
                "companion_data": data_no_conditions,
                "npc_hp_max": 30,
                "max_active_companions": 1,
            },
            headers=session_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "companion_limit"

    def test_recruit_character_not_found(self, db_client, session_header):
        resp = db_client.post(
            "/companions/recruit",
            json={
                "character_id": "nonexistent",
                "npc_id": "kaelen",
                "companion_data": COMPANION_DATA,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )
        assert resp.status_code == 404


class TestCombatActionEndpoint:
    def _recruit(self, db_client, session_header, character_id):
        data_no_conditions = {
            **COMPANION_DATA,
            "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"},
        }
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": data_no_conditions,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )

    def test_combat_action_attack(self, db_client, session_header, character_id):
        self._recruit(db_client, session_header, character_id)
        target = {"id": "goblin_1", "ac": 10, "hp_current": 15, "ability_scores": {}, "level": 1}

        with patch("random.randint", side_effect=[15, 4]):
            resp = db_client.post(
                "/companions/kaelen/combat-action",
                json={
                    "character_id": character_id,
                    "companion_data": COMPANION_DATA,
                    "target": target,
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        action = resp.json()["action"]
        assert action["action_type"] == "attack"
        assert action["hit"] is True

    def test_combat_action_companion_not_found(self, db_client, session_header, character_id):
        resp = db_client.post(
            "/companions/nonexistent/combat-action",
            json={
                "character_id": character_id,
                "companion_data": COMPANION_DATA,
            },
            headers=session_header,
        )
        assert resp.status_code == 404


class TestIncapacitateEndpoint:
    def _recruit(self, db_client, session_header, character_id):
        data_no_conditions = {
            **COMPANION_DATA,
            "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"},
        }
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": data_no_conditions,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )

    def test_incapacitate_companion(self, db_client, session_header, character_id):
        self._recruit(db_client, session_header, character_id)

        resp = db_client.post(
            "/companions/kaelen/incapacitate",
            json={
                "character_id": character_id,
                "companion_data": COMPANION_DATA,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["exhaustion_level"] == 1
        assert result["loyalty_strain"] == 1
        assert result["relationship_new"] == 50  # 60 + (-10)
        assert result["confrontation_triggered"] is False

    def test_incapacitate_increments_strain(self, db_client, session_header, character_id):
        self._recruit(db_client, session_header, character_id)

        for _ in range(2):
            db_client.post(
                "/companions/kaelen/incapacitate",
                json={"character_id": character_id, "companion_data": COMPANION_DATA},
                headers=session_header,
            )
            db_client.post(
                "/companions/kaelen/recover",
                json={"character_id": character_id, "companion_data": COMPANION_DATA},
                headers=session_header,
            )

        resp = db_client.post(
            "/companions/kaelen/incapacitate",
            json={"character_id": character_id, "companion_data": COMPANION_DATA},
            headers=session_header,
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["loyalty_strain"] == 3
        assert result["confrontation_triggered"] is True

    def test_incapacitate_persists_state(self, db_client, session_header, character_id):
        self._recruit(db_client, session_header, character_id)

        db_client.post(
            "/companions/kaelen/incapacitate",
            json={"character_id": character_id, "companion_data": COMPANION_DATA},
            headers=session_header,
        )

        resp = db_client.get(
            f"/companions/{character_id}",
            headers=session_header,
        )
        assert resp.status_code == 200
        companions = resp.json()["companions"]
        kaelen = next(c for c in companions if c["npc_id"] == "kaelen")
        assert kaelen["active"] is False
        assert kaelen["exhaustion_level"] == 1
        assert kaelen["loyalty_strain"] == 1


class TestDismissEndpoint:
    def _recruit(self, db_client, session_header, character_id):
        data_no_conditions = {
            **COMPANION_DATA,
            "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"},
        }
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": data_no_conditions,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )

    def test_dismiss_companion(self, db_client, session_header, character_id):
        self._recruit(db_client, session_header, character_id)

        resp = db_client.post(
            "/companions/kaelen/dismiss",
            json={
                "character_id": character_id,
                "companion_data": COMPANION_DATA,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["farewell_template"] == "farewell_kaelen"
        assert result["relationship_new"] == 50

        status_resp = db_client.get(
            f"/companions/{character_id}",
            headers=session_header,
        )
        assert len(status_resp.json()["companions"]) == 0


class TestGetCompanionsEndpoint:
    def test_get_empty_companions(self, db_client, session_header, character_id):
        resp = db_client.get(
            f"/companions/{character_id}",
            headers=session_header,
        )
        assert resp.status_code == 200
        assert resp.json()["companions"] == []

    def test_get_companions_after_recruit(self, db_client, session_header, character_id):
        data_no_conditions = {
            **COMPANION_DATA,
            "recruitment": {"affection_threshold": 10, "recruitment_scenario_id": "test"},
        }
        db_client.post(
            "/companions/recruit",
            json={
                "character_id": character_id,
                "npc_id": "kaelen",
                "companion_data": data_no_conditions,
                "npc_hp_max": 45,
            },
            headers=session_header,
        )

        resp = db_client.get(
            f"/companions/{character_id}",
            headers=session_header,
        )
        assert resp.status_code == 200
        companions = resp.json()["companions"]
        assert len(companions) == 1
        assert companions[0]["npc_id"] == "kaelen"
        assert companions[0]["hp_current"] == 45
        assert companions[0]["behavior_type"] == "aggressive"
        assert companions[0]["active"] is True
