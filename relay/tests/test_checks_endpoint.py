"""Tests for POST /checks/implicit and POST /checks/contested endpoints (step 10, #2, #6)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token


@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def session_header():
    token = create_session_token(
        player_id="player_001",
        world_id="inkglass_dark",
        session_id="sess_checks",
        tier=1,
        role="player",
        mode="solo",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def character_id(db_client, auth_header):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Andalu",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 10,
                "dexterity": 16,
                "constitution": 14,
                "intelligence": 12,
                "wisdom": 14,
                "charisma": 10,
            },
            "skill_proficiencies": ["stealth", "perception", "survival"],
            "saving_throw_proficiencies": ["dexterity", "wisdom"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def second_character_id(db_client, auth_header):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Renna",
            "specialisation_path_id": "scholar",
            "ability_scores": {
                "strength": 8,
                "dexterity": 10,
                "constitution": 12,
                "intelligence": 16,
                "wisdom": 14,
                "charisma": 12,
            },
            "skill_proficiencies": ["insight", "investigation", "arcana"],
            "saving_throw_proficiencies": ["intelligence", "wisdom"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


class TestImplicitChecks:
    def test_single_check(self, db_client, session_header, character_id):
        """Resolves a single implicit check against character stats."""
        with patch("relay.checks.resolver.random.randint", return_value=12):
            resp = db_client.post(
                "/checks/implicit",
                json={
                    "character_id": character_id,
                    "checks": [{"skill": "stealth", "dc": 15, "reason": "sneaking past guard"}],
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["checks_resolved"] == 1
        result = data["results"][0]
        assert result["skill"] == "stealth"
        assert result["dc"] == 15
        assert result["roll"] == 12
        # DEX 16 = +3, proficient stealth at level 1 = +2, total mod = 5
        assert result["modifier"] == 5
        assert result["total"] == 17
        assert result["passed"] is True

    def test_multiple_checks(self, db_client, session_header, character_id):
        """Resolves multiple checks in one request."""
        with patch("relay.checks.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/checks/implicit",
                json={
                    "character_id": character_id,
                    "checks": [
                        {"skill": "stealth", "dc": 15},
                        {"skill": "perception", "dc": 12},
                    ],
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        assert resp.json()["checks_resolved"] == 2

    def test_invalid_skill_defaults_to_perception(self, db_client, session_header, character_id):
        with patch("relay.checks.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/checks/implicit",
                json={
                    "character_id": character_id,
                    "checks": [{"skill": "lockpicking", "dc": 15}],
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        assert resp.json()["results"][0]["skill"] == "perception"

    def test_character_not_found(self, db_client, session_header):
        resp = db_client.post(
            "/checks/implicit",
            json={
                "character_id": "nonexistent_char",
                "checks": [{"skill": "stealth", "dc": 15}],
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_natural_20_flagged(self, db_client, session_header, character_id):
        with patch("relay.checks.resolver.random.randint", return_value=20):
            resp = db_client.post(
                "/checks/implicit",
                json={
                    "character_id": character_id,
                    "checks": [{"skill": "stealth", "dc": 15}],
                },
                headers=session_header,
            )
        assert resp.json()["results"][0]["natural_20"] is True

    def test_new_skills_accepted(self, db_client, session_header, character_id):
        """sleight_of_hand and animal_handling resolve without defaulting to perception."""
        with patch("relay.checks.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/checks/implicit",
                json={
                    "character_id": character_id,
                    "checks": [
                        {"skill": "sleight_of_hand", "dc": 12},
                        {"skill": "animal_handling", "dc": 12},
                    ],
                },
                headers=session_header,
            )
        results = resp.json()["results"]
        assert results[0]["skill"] == "sleight_of_hand"
        assert results[1]["skill"] == "animal_handling"


class TestContestedChecks:
    def test_contested_check_resolves(self, db_client, session_header, character_id, second_character_id):
        with patch("relay.checks.resolver.random.randint", side_effect=[18, 5]):
            resp = db_client.post(
                "/checks/contested",
                json={
                    "attacker_character_id": character_id,
                    "defender_character_id": second_character_id,
                    "attacker_skill": "stealth",
                    "defender_skill": "perception",
                    "reason": "sneaking past",
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["winner"] == "attacker"
        assert data["attacker"]["skill"] == "stealth"
        assert data["defender"]["skill"] == "perception"

    def test_contested_check_tie_goes_to_defender(self, db_client, session_header, character_id):
        """Same character on both sides, same roll → tie → defender wins."""
        with patch("relay.checks.resolver.random.randint", return_value=10):
            resp = db_client.post(
                "/checks/contested",
                json={
                    "attacker_character_id": character_id,
                    "defender_character_id": character_id,
                    "attacker_skill": "stealth",
                    "defender_skill": "stealth",
                    "reason": "mirror match",
                },
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["winner"] == "defender"
        assert data["tie"] is True

    def test_contested_check_missing_attacker(self, db_client, session_header, character_id):
        resp = db_client.post(
            "/checks/contested",
            json={
                "attacker_character_id": "nonexistent",
                "defender_character_id": character_id,
                "attacker_skill": "athletics",
                "defender_skill": "athletics",
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_contested_check_missing_defender(self, db_client, session_header, character_id):
        resp = db_client.post(
            "/checks/contested",
            json={
                "attacker_character_id": character_id,
                "defender_character_id": "nonexistent",
                "attacker_skill": "athletics",
                "defender_skill": "athletics",
            },
            headers=session_header,
        )
        assert resp.status_code == 404
