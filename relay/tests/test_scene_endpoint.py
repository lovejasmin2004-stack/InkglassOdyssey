"""Tests for scene lifecycle endpoints: POST /scene, GET, end, PATCH."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token, create_session_token
from relay.tests.conftest import make_stub_npc


@pytest.fixture(autouse=True)
def _mock_load_npc():
    with patch("relay.endpoints.scene.load_npc", return_value=make_stub_npc()):
        yield


@pytest.fixture()
def auth_header():
    return {"Authorization": f"Bearer {create_account_token(player_id='p1', tier=1)}"}


@pytest.fixture()
def other_auth():
    return {"Authorization": f"Bearer {create_account_token(player_id='p2', tier=1)}"}


@pytest.fixture()
def dm_header():
    return {
        "Authorization": f"Bearer {create_session_token(player_id='p1', world_id='inkglass_dark', session_id='s1', tier=1, role='dm', mode='solo')}"
    }


@pytest.fixture()
def character_id(db_client, auth_header):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Test",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 10,
                "dexterity": 14,
                "constitution": 12,
                "intelligence": 10,
                "wisdom": 14,
                "charisma": 10,
            },
            "skill_proficiencies": ["perception"],
            "saving_throw_proficiencies": ["dexterity", "wisdom"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def session_id(db_client, auth_header, character_id):
    resp = db_client.post(
        "/session/start",
        json={"character_id": character_id, "world_id": "inkglass_dark"},
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


@pytest.fixture()
def scene_id(db_client, auth_header, session_id):
    resp = db_client.post(
        "/scene",
        json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


class TestSceneCreate:
    def test_create_scene(self, db_client, auth_header, session_id):
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["session_id"] == session_id
        assert data["npc_id"] == "seta_inkglass_dark"
        assert data["mode"] == "rp"
        assert data["status"] == "active"
        assert data["turn_count"] == 0

    def test_create_scene_quickchat_mode(self, db_client, auth_header, session_id):
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "quickchat"},
            headers=auth_header,
        )
        assert resp.status_code == 201
        assert resp.json()["mode"] == "quickchat"

    def test_create_scene_invalid_mode_returns_422(self, db_client, auth_header, session_id):
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "combat"},
            headers=auth_header,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_create_scene_session_not_found(self, db_client, auth_header):
        resp = db_client.post(
            "/scene",
            json={"session_id": "nonexistent", "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_create_scene_other_player_session(self, db_client, auth_header, other_auth, session_id):
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=other_auth,
        )
        assert resp.status_code == 403

    def test_create_scene_ended_session(self, db_client, auth_header, session_id):
        db_client.post(f"/session/{session_id}/end", json={}, headers=auth_header)
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "session_ended"

    def test_create_scene_npc_not_found(self, db_client, auth_header, session_id):
        with patch("relay.endpoints.scene.load_npc", return_value=None):
            resp = db_client.post(
                "/scene",
                json={"session_id": session_id, "npc_id": "nobody"},
                headers=auth_header,
            )
        assert resp.status_code == 404


class TestSceneGet:
    def test_get_scene(self, db_client, auth_header, scene_id):
        resp = db_client.get(f"/scene/{scene_id}", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == scene_id
        assert data["status"] == "active"

    def test_get_scene_not_found(self, db_client, auth_header):
        resp = db_client.get("/scene/nonexistent", headers=auth_header)
        assert resp.status_code == 404

    def test_get_scene_other_player(self, db_client, auth_header, other_auth, scene_id):
        resp = db_client.get(f"/scene/{scene_id}", headers=other_auth)
        assert resp.status_code == 403


class TestSceneEnd:
    def test_end_scene(self, db_client, auth_header, scene_id):
        resp = db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ended"
        assert data["ended_at"] is not None
        assert data["scene_summary"] is not None

    def test_end_scene_not_found(self, db_client, auth_header):
        resp = db_client.post("/scene/nonexistent/end", headers=auth_header)
        assert resp.status_code == 404

    def test_end_scene_already_ended(self, db_client, auth_header, scene_id):
        db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        resp = db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        assert resp.status_code == 409
        assert resp.json()["code"] == "scene_ended"

    def test_end_scene_other_player(self, db_client, auth_header, other_auth, scene_id):
        resp = db_client.post(f"/scene/{scene_id}/end", headers=other_auth)
        assert resp.status_code == 403

    def test_ended_scene_appears_in_get(self, db_client, auth_header, scene_id):
        db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        resp = db_client.get(f"/scene/{scene_id}", headers=auth_header)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ended"


class TestScenePatch:
    def test_patch_scene_state_as_dm(self, db_client, dm_header, auth_header, session_id):
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        resp = db_client.patch(
            f"/scene/{scene_id}",
            json={"scene_state": {"emotional_temperature": 0.9}},
            headers=dm_header,
        )
        assert resp.status_code == 200
        assert resp.json()["scene_state"]["emotional_temperature"] == 0.9

    def test_patch_hidden_elements(self, db_client, dm_header, auth_header, session_id):
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        hidden = [{"id": "trap_1", "dc": 15, "skill": "perception", "hint": "You notice a wire."}]
        resp = db_client.patch(
            f"/scene/{scene_id}",
            json={"hidden_elements": hidden},
            headers=dm_header,
        )
        assert resp.status_code == 200
        assert resp.json()["scene_state"]["hidden_elements"] == hidden

    def test_patch_environmental_effects(self, db_client, dm_header, auth_header, session_id):
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        resp = db_client.patch(
            f"/scene/{scene_id}",
            json={"environmental_effects": ["darkness", "difficult_terrain"]},
            headers=dm_header,
        )
        assert resp.status_code == 200
        effects = resp.json()["scene_state"]["environmental_effects"]
        assert "darkness" in effects
        assert "difficult_terrain" in effects

    def test_patch_scene_not_dm_returns_403(self, db_client, auth_header, scene_id):
        import relay.config as _config

        original = _config.settings.admin_mode
        _config.settings.admin_mode = False
        try:
            resp = db_client.patch(
                f"/scene/{scene_id}",
                json={"scene_state": {"foo": "bar"}},
                headers=auth_header,
            )
            assert resp.status_code == 403
        finally:
            _config.settings.admin_mode = original

    def test_patch_ended_scene_returns_409(self, db_client, dm_header, auth_header, scene_id):
        db_client.post(f"/scene/{scene_id}/end", headers=auth_header)
        resp = db_client.patch(
            f"/scene/{scene_id}",
            json={"scene_state": {"foo": "bar"}},
            headers=dm_header,
        )
        assert resp.status_code == 409

    def test_patch_scene_not_found(self, db_client, dm_header):
        resp = db_client.patch(
            "/scene/nonexistent",
            json={"scene_state": {"foo": "bar"}},
            headers=dm_header,
        )
        assert resp.status_code == 404
