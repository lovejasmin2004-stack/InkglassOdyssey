"""Tests for combat HTTP endpoints: attack, save, initiative, heal, tick-conditions, rest."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token


@pytest.fixture()
def auth_header():
    return {"Authorization": f"Bearer {create_account_token(player_id='p1', tier=1)}"}


@pytest.fixture()
def session_header():
    return {
        "Authorization": f"Bearer {create_session_token(player_id='p1', world_id='inkglass_dark', session_id='s1', tier=1, role='player', mode='solo')}"
    }


@pytest.fixture()
def other_session_header():
    return {
        "Authorization": f"Bearer {create_session_token(player_id='p2', world_id='inkglass_dark', session_id='s2', tier=1, role='player', mode='solo')}"
    }


def _create_character(db_client, auth_header, *, name="Fighter", hp_max=None, ac=None):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": name,
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 16,
                "dexterity": 14,
                "constitution": 14,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 8,
            },
            "skill_proficiencies": ["athletics"],
            "saving_throw_proficiencies": ["strength", "constitution"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    cid = resp.json()["id"]

    patches = {}
    if hp_max is not None:
        patches["hp_max"] = hp_max
        patches["hp_current"] = hp_max
    if ac is not None:
        patches["ac"] = ac
    if patches:
        resp = db_client.patch(f"/character/{cid}", json=patches, headers=auth_header)
        assert resp.status_code == 200

    return cid


@pytest.fixture()
def attacker_id(db_client, auth_header):
    return _create_character(db_client, auth_header, name="Attacker", hp_max=30)


@pytest.fixture()
def target_id(db_client, auth_header):
    return _create_character(db_client, auth_header, name="Target", hp_max=30, ac=12)


_WEAPON = {
    "ability": "strength",
    "damage_dice": "1d8",
    "damage_type": "slashing",
    "damage_bonus": 0,
}


class TestAttackEndpoint:
    def test_attack_returns_200(self, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon": _WEAPON,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "hit" in data
        assert "critical" in data
        assert "attack_roll" in data

    @patch("relay.combat.resolver.random.randint", return_value=20)
    def test_attack_critical_hit(self, _mock, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon": _WEAPON,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is True
        assert data["critical"] is True
        assert data["damage"] is not None

    @patch("relay.combat.resolver.random.randint", return_value=1)
    def test_attack_auto_miss(self, _mock, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon": _WEAPON,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is False
        assert data["auto_miss"] is True

    def test_attack_reduces_target_hp(self, db_client, session_header, auth_header, attacker_id, target_id):
        with patch("relay.combat.resolver.random.randint", return_value=18):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": attacker_id,
                    "target_id": target_id,
                    "weapon": _WEAPON,
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        if data["hit"]:
            assert data["target_hp_after"] is not None
            char_resp = db_client.get(f"/character/{target_id}", headers=auth_header)
            assert char_resp.json()["hp_current"] == data["target_hp_after"]

    def test_attack_attacker_not_found(self, db_client, session_header, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": "nonexistent",
                "target_id": target_id,
                "weapon": _WEAPON,
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_attack_target_not_found(self, db_client, session_header, attacker_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": "nonexistent",
                "weapon": _WEAPON,
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_attack_requires_session_token(self, db_client, auth_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon": _WEAPON,
            },
            headers=auth_header,
        )
        assert resp.status_code == 403

    def test_attack_with_environmental_effects(self, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/attack",
            json={
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon": _WEAPON,
                "environmental_effects": ["darkness"],
            },
            headers=session_header,
        )
        assert resp.status_code == 200

    def test_attack_death_state(self, db_client, session_header, auth_header, attacker_id, target_id):
        db_client.patch(
            f"/character/{target_id}",
            json={"hp_current": 1},
            headers=auth_header,
        )
        with patch("relay.combat.resolver.random.randint", return_value=20):
            resp = db_client.post(
                "/combat/attack",
                json={
                    "attacker_id": attacker_id,
                    "target_id": target_id,
                    "weapon": _WEAPON,
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit"] is True
        if data["target_hp_after"] == 0:
            assert data["target_entered_death_state"] is True


class TestSaveEndpoint:
    def test_save_returns_200(self, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/save",
            json={
                "attacker_id": attacker_id,
                "defender_id": target_id,
                "save_type": "dexterity",
                "dc_source_ability": "intelligence",
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "passed" in data
        assert "save_roll" in data

    def test_save_with_damage(self, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/save",
            json={
                "attacker_id": attacker_id,
                "defender_id": target_id,
                "save_type": "dexterity",
                "dc_source_ability": "intelligence",
                "damage_dice": "2d6",
                "damage_type": "fire",
                "half_on_save": True,
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["damage"] is not None or data["passed"]

    def test_save_applies_condition_on_fail(self, db_client, session_header, attacker_id, target_id):
        with patch("relay.combat.resolver.random.randint", return_value=1):
            resp = db_client.post(
                "/combat/save",
                json={
                    "attacker_id": attacker_id,
                    "defender_id": target_id,
                    "save_type": "constitution",
                    "dc_source_ability": "intelligence",
                    "applies_condition": {"condition_id": "poisoned", "duration": 3},
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        if not data["passed"]:
            assert data["condition_applied"] == "poisoned"

    def test_save_defender_not_found(self, db_client, session_header, attacker_id):
        resp = db_client.post(
            "/combat/save",
            json={
                "attacker_id": attacker_id,
                "defender_id": "nonexistent",
                "save_type": "wisdom",
                "dc_source_ability": "charisma",
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_save_requires_session_token(self, db_client, auth_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/save",
            json={
                "attacker_id": attacker_id,
                "defender_id": target_id,
                "save_type": "dexterity",
                "dc_source_ability": "intelligence",
            },
            headers=auth_header,
        )
        assert resp.status_code == 403


class TestInitiativeEndpoint:
    def test_initiative_returns_turn_order(self, db_client, session_header, attacker_id, target_id):
        resp = db_client.post(
            "/combat/initiative",
            json={"participant_ids": [attacker_id, target_id]},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["turn_order"]) == 2
        ids = [e["participant_id"] for e in data["turn_order"]]
        assert set(ids) == {attacker_id, target_id}

    def test_initiative_participant_not_found(self, db_client, session_header, attacker_id):
        resp = db_client.post(
            "/combat/initiative",
            json={"participant_ids": [attacker_id, "nonexistent"]},
            headers=session_header,
        )
        assert resp.status_code == 404


class TestHealEndpoint:
    def test_heal_restores_hp(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 10},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/heal",
            json={"target_id": attacker_id, "healing": 5},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hp_after"] == 15
        assert data["left_death_state"] is False

    def test_heal_capped_at_max(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 28},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/heal",
            json={"target_id": attacker_id, "healing": 100},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hp_after"] == data["hp_max"]

    def test_heal_from_death_state(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 0},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/heal",
            json={"target_id": attacker_id, "healing": 10},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hp_after"] > 0
        assert data["left_death_state"] is True

    def test_heal_not_found(self, db_client, session_header):
        resp = db_client.post(
            "/combat/heal",
            json={"target_id": "nonexistent", "healing": 5},
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_heal_requires_session_token(self, db_client, auth_header, attacker_id):
        resp = db_client.post(
            "/combat/heal",
            json={"target_id": attacker_id, "healing": 5},
            headers=auth_header,
        )
        assert resp.status_code == 403


class TestTickConditionsEndpoint:
    def test_tick_conditions(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={
                "conditions": [
                    {
                        "condition_id": "poisoned",
                        "source": "venom",
                        "source_type": "spell",
                        "duration_remaining": 2,
                        "duration_unit": "turns",
                    }
                ]
            },
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": attacker_id, "current_turn": 1},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["character_id"] == attacker_id
        assert "conditions" in data

    def test_tick_conditions_death_state(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 0},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": attacker_id, "current_turn": 1},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["death_state"] is not None

    def test_tick_conditions_not_found(self, db_client, session_header):
        resp = db_client.post(
            "/combat/tick-conditions",
            json={"character_id": "nonexistent", "current_turn": 1},
            headers=session_header,
        )
        assert resp.status_code == 404


class TestRestEndpoint:
    def test_long_rest_restores_hp(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 10},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": attacker_id, "rest_type": "long"},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rest_type"] == "long"
        assert data["hp_after"] == data["hp_max"]
        assert data["hp_before"] == 10

    def test_long_rest_reduces_exhaustion(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"exhaustion_level": 2},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": attacker_id, "rest_type": "long"},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exhaustion_after"] < data["exhaustion_before"]

    def test_short_rest_no_hp_change(self, db_client, session_header, auth_header, attacker_id):
        db_client.patch(
            f"/character/{attacker_id}",
            json={"hp_current": 15},
            headers=auth_header,
        )
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": attacker_id, "rest_type": "short"},
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rest_type"] == "short"
        assert data["hp_after"] == 15

    def test_rest_invalid_type_returns_422(self, db_client, session_header, attacker_id):
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": attacker_id, "rest_type": "medium"},
            headers=session_header,
        )
        assert resp.status_code == 422

    def test_rest_not_found(self, db_client, session_header):
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": "nonexistent", "rest_type": "long"},
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_rest_requires_session_token(self, db_client, auth_header, attacker_id):
        resp = db_client.post(
            "/combat/rest",
            json={"character_id": attacker_id, "rest_type": "long"},
            headers=auth_header,
        )
        assert resp.status_code == 403
