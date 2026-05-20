"""Tests for POST /dice/roll endpoint (step 10, #3)."""

from __future__ import annotations

from unittest.mock import patch


class TestDiceRoll:
    def test_simple_d20(self, db_client, session_header):
        with patch("relay.endpoints.dice.random.randint", return_value=15):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "1d20"},
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["notation"] == "1d20"
        assert len(data["rolls"]) == 1
        assert data["rolls"][0]["dice"] == [15]
        assert data["rolls"][0]["total"] == 15
        assert data["grand_total"] == 15

    def test_2d6_roll(self, db_client, session_header):
        with patch("relay.endpoints.dice.random.randint", side_effect=[3, 5]):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "2d6"},
                headers=session_header,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rolls"][0]["dice"] == [3, 5]
        assert data["rolls"][0]["total"] == 8
        assert data["grand_total"] == 8

    def test_modifier_positive(self, db_client, session_header):
        with patch("relay.endpoints.dice.random.randint", return_value=10):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "1d20+5"},
                headers=session_header,
            )
        data = resp.json()
        assert data["rolls"][0]["modifier"] == 5
        assert data["rolls"][0]["total"] == 15

    def test_modifier_negative(self, db_client, session_header):
        with patch("relay.endpoints.dice.random.randint", return_value=10):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "1d20-3"},
                headers=session_header,
            )
        data = resp.json()
        assert data["rolls"][0]["modifier"] == -3
        assert data["rolls"][0]["total"] == 7

    def test_multiple_count(self, db_client, session_header):
        """count > 1 repeats the full roll."""
        with patch("relay.endpoints.dice.random.randint", return_value=4):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "1d6", "count": 3},
                headers=session_header,
            )
        data = resp.json()
        assert len(data["rolls"]) == 3
        assert data["grand_total"] == 12  # 4 * 3

    def test_reason_field(self, db_client, session_header):
        with patch("relay.endpoints.dice.random.randint", return_value=10):
            resp = db_client.post(
                "/dice/roll",
                json={"notation": "1d20", "reason": "initiative"},
                headers=session_header,
            )
        assert resp.json()["reason"] == "initiative"

    def test_invalid_notation_rejected(self, db_client, session_header):
        resp = db_client.post(
            "/dice/roll",
            json={"notation": "not_dice"},
            headers=session_header,
        )
        assert resp.status_code == 422  # validation error from regex

    def test_requires_auth(self, db_client):
        resp = db_client.post(
            "/dice/roll",
            json={"notation": "1d20"},
        )
        assert resp.status_code == 401
