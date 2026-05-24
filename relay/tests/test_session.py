"""Tests for session and scene lifecycle endpoints."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select as sa_select

import relay.database as _db
from relay.auth.tokens import create_account_token
from relay.models import Character as CharacterModel
from relay.models import Scene as SceneModel
from relay.tests.conftest import make_stub_npc


@pytest.fixture(autouse=True)
def _mock_load_npc():
    with patch("relay.endpoints.scene.load_npc", return_value=make_stub_npc()):
        yield


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
        # Pydantic validation returns 422 for missing required field
        assert resp.status_code == 422

    def test_start_scene_invalid_mode(self, db_client, auth_header, character_id):
        session_id = self._start_session(db_client, auth_header, character_id)

        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta", "mode": "combat"},
            headers=auth_header,
        )
        # Pydantic pattern validation returns 422 for invalid mode value
        assert resp.status_code == 422

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
        db_client.post(
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

        # Create a scene and manually add turns to meet the minimum threshold (5)
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        # Patch the scene's turn_count directly via DB to simulate gameplay
        import asyncio

        from sqlalchemy import select as sa_select

        import relay.database as _db
        from relay.models import Scene as SceneModel

        async def _set_turn_count():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(SceneModel).where(SceneModel.id == scene_id))
                scene = result.scalar_one()
                scene.turn_count = 6
                await db.commit()

        asyncio.run(_set_turn_count())

        resp = db_client.post(f"/session/{session_id}/end", json={"level_increment": True}, headers=auth_header)
        assert resp.status_code == 200

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


# ---------------------------------------------------------------------------
# Tier 2 world enforcement (#1)
# ---------------------------------------------------------------------------


class TestTier2WorldEnforcement:
    """Tier 2 worlds (wha_au, atla_au, gachiakuta_au, hxh_au) reject tier 1 players."""

    @pytest.fixture()
    def tier2_auth_header(self):
        token = create_account_token(player_id="player_001", tier=2)
        return {"Authorization": f"Bearer {token}"}

    @pytest.fixture()
    def tier2_character_id(self, db_client, tier2_auth_header):
        """Create a character in a tier 2 world."""
        resp = db_client.post(
            "/character",
            json={
                "world_id": "wha_au",
                "name": "Tier2Char",
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
            headers=tier2_auth_header,
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_tier1_player_rejected_from_tier2_world(self, db_client, auth_header, tier2_character_id):
        """Tier 1 player cannot start session in a tier 2 world."""
        resp = db_client.post(
            "/session/start",
            json={"character_id": tier2_character_id, "world_id": "wha_au"},
            headers=auth_header,  # tier=1
        )
        assert resp.status_code == 403
        assert "Tier 2" in resp.json()["message"]

    def test_tier2_player_allowed_in_tier2_world(self, db_client, tier2_auth_header, tier2_character_id):
        """Tier 2 player can start session in a tier 2 world."""
        resp = db_client.post(
            "/session/start",
            json={"character_id": tier2_character_id, "world_id": "wha_au"},
            headers=tier2_auth_header,
        )
        assert resp.status_code == 201
        assert resp.json()["world_id"] == "wha_au"

    @pytest.mark.parametrize("world_id", ["wha_au", "atla_au", "gachiakuta_au", "hxh_au"])
    def test_all_tier2_worlds_reject_tier1(self, db_client, auth_header, world_id):
        """All four tier 2 worlds enforce tier restriction."""
        resp = db_client.post(
            "/session/start",
            json={"character_id": "dummy_char", "world_id": world_id},
            headers=auth_header,  # tier=1
        )
        # Rejected at tier check before character lookup
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# World mismatch (#2)
# ---------------------------------------------------------------------------


class TestWorldMismatch:
    """Character's world_id must match the session's requested world_id."""

    def test_character_world_mismatch_rejected(self, db_client, auth_header, character_id):
        """Character in inkglass_dark cannot start session in murim."""
        resp = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "murim"},
            headers=auth_header,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == "world_mismatch"
        assert "inkglass_dark" in data["message"]
        assert "murim" in data["message"]


# ---------------------------------------------------------------------------
# Scene max limit (#5)
# ---------------------------------------------------------------------------


class TestSceneMaxLimit:
    """Maximum 20 active scenes per session."""

    def test_scene_limit_reached(self, db_client, auth_header, character_id):
        """21st scene creation returns 409 scene_limit_reached."""
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        # Create 20 active scenes (the maximum)
        for i in range(20):
            resp = db_client.post(
                "/scene",
                json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
                headers=auth_header,
            )
            assert resp.status_code == 201, f"Scene {i + 1} creation failed"

        # 21st should fail
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "scene_limit_reached"

    def test_ended_scenes_dont_count_toward_limit(self, db_client, auth_header, character_id):
        """Ended scenes free up space for new ones."""
        start = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = start.json()["session_id"]

        # Create and end 20 scenes
        for _ in range(20):
            create = db_client.post(
                "/scene",
                json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
                headers=auth_header,
            )
            scene_id = create.json()["id"]
            db_client.post(f"/scene/{scene_id}/end", headers=auth_header)

        # Can still create a new scene because all 20 are ended
        resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Level-up validation (#9)
# ---------------------------------------------------------------------------


class TestLevelUpValidation:
    """Level increment requires minimum turn count and respects level cap."""

    def _start_session(self, db_client, auth_header, character_id):
        resp = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        return resp.json()["session_id"]

    def test_level_up_insufficient_turns_rejected(self, db_client, auth_header, character_id):
        """Level increment rejected when total turns < 5."""
        session_id = self._start_session(db_client, auth_header, character_id)

        # Create a scene with only 3 turns (below the 5 minimum)
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        async def _set_turn_count():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(SceneModel).where(SceneModel.id == scene_id))
                scene = result.scalar_one()
                scene.turn_count = 3
                await db.commit()

        asyncio.run(_set_turn_count())

        resp = db_client.post(
            f"/session/{session_id}/end",
            json={"level_increment": True},
            headers=auth_header,
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["code"] == "insufficient_progress"
        assert "5 turns" in data["message"]

    def test_level_up_insufficient_turns_keeps_session_active(self, db_client, auth_header, character_id):
        """Failed level_increment validation leaves session active for retry."""
        session_id = self._start_session(db_client, auth_header, character_id)

        # Only 2 turns — below threshold
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        async def _set_turn_count():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(SceneModel).where(SceneModel.id == scene_id))
                scene = result.scalar_one()
                scene.turn_count = 2
                await db.commit()

        asyncio.run(_set_turn_count())

        # Attempt level-up — fails
        resp = db_client.post(
            f"/session/{session_id}/end",
            json={"level_increment": True},
            headers=auth_header,
        )
        assert resp.status_code == 400

        # Session is still active — can retry with level_increment=false
        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert state.json()["status"] == "active"

        # End without level increment succeeds
        resp2 = db_client.post(
            f"/session/{session_id}/end",
            json={"level_increment": False},
            headers=auth_header,
        )
        assert resp2.status_code == 200

    def test_level_up_at_cap_does_not_increment(self, db_client, auth_header, character_id):
        """Character at level 20 does not exceed cap."""
        session_id = self._start_session(db_client, auth_header, character_id)

        # Set character to level 20 (the cap)
        async def _set_level_to_cap():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(CharacterModel).where(CharacterModel.id == character_id))
                char = result.scalar_one()
                char.level = 20
                await db.commit()

        asyncio.run(_set_level_to_cap())

        # Create a scene with enough turns
        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        async def _set_turn_count():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(SceneModel).where(SceneModel.id == scene_id))
                scene = result.scalar_one()
                scene.turn_count = 10
                await db.commit()

        asyncio.run(_set_turn_count())

        resp = db_client.post(
            f"/session/{session_id}/end",
            json={"level_increment": True},
            headers=auth_header,
        )
        assert resp.status_code == 200

        # Character remains at level 20, not 21
        char_resp = db_client.get(f"/character/{character_id}", headers=auth_header)
        assert char_resp.json()["level"] == 20

    def test_level_up_exactly_at_threshold(self, db_client, auth_header, character_id):
        """Level up succeeds when total turns == exactly 5 (the minimum)."""
        session_id = self._start_session(db_client, auth_header, character_id)

        scene_resp = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene_resp.json()["id"]

        async def _set_turn_count():
            async with _db.AsyncSessionLocal() as db:
                result = await db.execute(sa_select(SceneModel).where(SceneModel.id == scene_id))
                scene = result.scalar_one()
                scene.turn_count = 5
                await db.commit()

        asyncio.run(_set_turn_count())

        resp = db_client.post(
            f"/session/{session_id}/end",
            json={"level_increment": True},
            headers=auth_header,
        )
        assert resp.status_code == 200

        char_resp = db_client.get(f"/character/{character_id}", headers=auth_header)
        assert char_resp.json()["level"] == 2
