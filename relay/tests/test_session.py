"""Tests for session and scene lifecycle endpoints."""
from __future__ import annotations

import pytest

from relay.auth.tokens import create_account_token


@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_header():
    token = create_account_token(player_id="player_002", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def character_id(db_client, auth_header):
    """Create a character and return its ID."""
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


# ---------------------------------------------------------------------------
# Session start
# ---------------------------------------------------------------------------

class TestSessionStart:
    def test_start_session(self, db_client, auth_header, character_id):
        resp = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"]
        assert data["session_token"]
        assert data["world_id"] == "inkglass_dark"
        assert data["mode"] == "solo"
        assert data["role"] == "player"

    def test_start_session_character_not_found(self, db_client, auth_header):
        resp = db_client.post(
            "/session/start",
            json={"character_id": "nonexistent", "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_start_session_character_belongs_to_other(self, db_client, auth_header, other_auth_header, character_id):
        resp = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=other_auth_header,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"

    def test_start_session_duplicate_active(self, db_client, auth_header, character_id):
        resp1 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        assert resp1.status_code == 201

        resp2 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        assert resp2.status_code == 409
        assert resp2.json()["code"] == "session_active"

    def test_start_session_no_auth(self, db_client):
        resp = db_client.post(
            "/session/start",
            json={"character_id": "x", "world_id": "inkglass_dark"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class TestSessionState:
    def test_get_session_state(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        resp = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["status"] == "active"
        assert data["scenes"] == []

    def test_get_session_state_not_found(self, db_client, auth_header):
        resp = db_client.get("/session/nonexistent/state", headers=auth_header)
        assert resp.status_code == 404

    def test_get_session_state_wrong_player(self, db_client, auth_header, other_auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        resp = db_client.get(f"/session/{session_id}/state", headers=other_auth_header)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Scene lifecycle
# ---------------------------------------------------------------------------

class TestSceneLifecycle:
    def _start_session(self, db_client, auth_header, character_id):
        resp = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        return resp.json()["session_id"]

    def test_start_scene(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["npc_id"] == "seta_inkglass_dark"
        assert data["mode"] == "rp"
        assert data["status"] == "active"
        assert data["turn_count"] == 0

    def test_start_scene_missing_fields(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        resp = db_client.post(
            "/scene",
            json={"session_id": session_id},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_field"

    def test_start_scene_invalid_mode(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta", "mode": "combat"},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_field"

    def test_start_scene_ended_session(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)

        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta", "mode": "rp"},
            headers=auth_header,
        )
        assert resp.status_code == 409

    def test_get_scene(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        create = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = create.json()["id"]

        resp = db_client.get(f"/scene/{scene_id}", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["id"] == scene_id

    def test_get_scene_not_found(self, db_client, auth_header):
        resp = db_client.get("/scene/nonexistent", headers=auth_header)
        assert resp.status_code == 404

    def test_end_scene(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        create = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = create.json()["id"]

        resp = db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ended"
        assert data["ended_at"] is not None
        assert data["scene_summary"] is not None

    def test_end_scene_already_ended(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        create = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = create.json()["id"]

        db_client.post(f"/scene/{scene_id}/end", headers=auth_header)

        resp = db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        assert resp.status_code == 409
        assert resp.json()["code"] == "scene_ended"


# ---------------------------------------------------------------------------
# Session end
# ---------------------------------------------------------------------------

class TestSessionEnd:
    def test_end_session_basic(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        resp = db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ended"
        assert data["session_summary"]
        assert data["analytics"]["scene_count"] == 0
        assert data["ended_at"] is not None

    def test_end_session_with_scenes(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        # Start and end a scene
        scene = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene.json()["id"]
        db_client.post(f"/scene/{scene_id}/end", headers=auth_header)

        # Start another scene (leave active -- session end should close it)
        scene2 = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "merchant_inkglass_dark", "mode": "quickchat"},
            headers=auth_header,
        )

        resp = db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["analytics"]["scene_count"] == 2
        assert data["scenes_ended"] == 1  # only the still-active one
        assert "seta_inkglass_dark" in data["session_summary"]

    def test_end_session_with_level_increment(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        db_client.post(f"/session/{session_id}/end", json={"level_increment": True}, headers=auth_header)

        char = db_client.get(f"/character/{character_id}", headers=auth_header)
        assert char.json()["level"] == 2

    def test_end_session_already_ended(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)

        resp = db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)
        assert resp.status_code == 409
        assert resp.json()["code"] == "session_ended"

    def test_end_session_not_found(self, db_client, auth_header):
        resp = db_client.post("/session/nonexistent/end", json={}, headers=auth_header)
        assert resp.status_code == 404

    def test_end_session_wrong_player(self, db_client, auth_header, other_auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        resp = db_client.post(f"/session/{session_id}/end", json={}, headers=other_auth_header)
        assert resp.status_code == 403

    def test_can_start_new_session_after_ending(self, db_client, auth_header, character_id):
        start1 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start1.json()["session_id"]
        db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)

        start2 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        assert start2.status_code == 201


# ---------------------------------------------------------------------------
# Session state includes scenes
# ---------------------------------------------------------------------------

class TestSessionStateWithScenes:
    def test_state_includes_scenes(self, db_client, auth_header, character_id):
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )

        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert state.status_code == 200
        data = state.json()
        assert len(data["scenes"]) == 1
        assert data["scenes"][0]["npc_id"] == "seta_inkglass_dark"
