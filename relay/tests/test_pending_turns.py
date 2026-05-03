"""Tests for pending turn persistence and recovery."""
from __future__ import annotations

import asyncio

import pytest

from relay.auth.tokens import create_account_token


@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
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
def session_and_scene(db_client, auth_header, character_id):
    """Create a session and scene, return (session_id, scene_id)."""
    sess = db_client.post(
        "/session/start",
        json={"character_id": character_id, "world_id": "inkglass_dark"},
        headers=auth_header,
    )
    assert sess.status_code == 201
    session_id = sess.json()["session_id"]

    scene = db_client.post(
        "/scene",
        json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
        headers=auth_header,
    )
    assert scene.status_code == 201
    scene_id = scene.json()["id"]

    return session_id, scene_id


class TestPendingTurnPersistence:
    def test_create_and_retrieve_pending_turn(self, db_client, auth_header, session_and_scene):
        """Pending turn created, stage updated, and retrievable via session state."""
        session_id, scene_id = session_and_scene

        # Directly test the persistence module
        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turns,
            update_stage,
        )

        turn_id = asyncio.run(create_pending_turn(
            scene_id=scene_id,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="Andalu looks around the room.",
            character_snapshot={"level": 6},
        ))
        assert turn_id.startswith("pt_")

        # Verify it appears in session state
        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert state.status_code == 200
        pending = state.json()["pending_turns"]
        assert len(pending) == 1
        assert pending[0]["turn_id"] == turn_id
        assert pending[0]["stage"] == "received"
        assert pending[0]["player_input"] == "Andalu looks around the room."

    def test_stage_progression(self, db_client, auth_header, session_and_scene):
        """Verify stage updates are persisted."""
        session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turn,
            update_stage,
        )

        turn_id = asyncio.run(create_pending_turn(
            scene_id=scene_id,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="She examines the vials.",
        ))

        # Progress through stages
        asyncio.run(update_stage(turn_id, "analysis"))
        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "analysis"

        asyncio.run(update_stage(
            turn_id, "checks_resolved",
            analysis_result={"checks": [{"skill": "perception", "dc": 14}]},
        ))
        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "checks_resolved"
        assert pt["analysis_result"]["checks"][0]["skill"] == "perception"

        asyncio.run(update_stage(
            turn_id, "streaming",
            check_results=[{"skill": "perception", "dc": 14, "passed": True, "roll": 18, "modifier": 5, "total": 23}],
        ))
        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "streaming"
        assert pt["check_results"][0]["passed"] is True

    def test_complete_turn_updates_scene(self, db_client, auth_header, session_and_scene):
        """Completing a turn should increment scene turn_count and add to history."""
        session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            complete_turn,
            create_pending_turn,
        )

        turn_id = asyncio.run(create_pending_turn(
            scene_id=scene_id,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="She opens the cabinet.",
        ))

        asyncio.run(complete_turn(turn_id, "Seta watches silently."))

        # Check scene was updated
        scene = db_client.get(f"/scene/{scene_id}", headers=auth_header)
        assert scene.status_code == 200
        data = scene.json()
        assert data["turn_count"] == 1

        # Completed turn should NOT appear in pending_turns
        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert len(state.json()["pending_turns"]) == 0

    def test_failed_turn_not_in_pending(self, db_client, auth_header, session_and_scene):
        """Failed turns should not appear in pending turns list."""
        session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            fail_turn,
        )

        turn_id = asyncio.run(create_pending_turn(
            scene_id=scene_id,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="Test input.",
        ))

        asyncio.run(fail_turn(turn_id, "API error"))

        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        assert len(state.json()["pending_turns"]) == 0

    def test_interrupted_turn_visible_in_state(self, db_client, auth_header, session_and_scene):
        """A turn stuck in 'analysis' stage should be visible for recovery."""
        session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            update_stage,
        )

        turn_id = asyncio.run(create_pending_turn(
            scene_id=scene_id,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="Andalu reaches for the book.",
        ))

        # Simulate crash after analysis stage
        asyncio.run(update_stage(
            turn_id, "analysis",
            analysis_result={"draft_response": "Seta frowns."},
        ))

        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        pending = state.json()["pending_turns"]
        assert len(pending) == 1
        assert pending[0]["turn_id"] == turn_id
        assert pending[0]["stage"] == "analysis"

    def test_multiple_scenes_separate_pending(self, db_client, auth_header, character_id):
        """Pending turns are scoped to their scene."""
        sess = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = sess.json()["session_id"]

        scene1 = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        ).json()["id"]

        scene2 = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "merchant_inkglass_dark", "mode": "quickchat"},
            headers=auth_header,
        ).json()["id"]

        from relay.persistence.pending_turns import create_pending_turn

        asyncio.run(create_pending_turn(
            scene_id=scene1,
            player_id="player_001",
            npc_id="seta_inkglass_dark",
            turn_type="rp",
            player_input="Turn in scene 1",
        ))
        asyncio.run(create_pending_turn(
            scene_id=scene2,
            player_id="player_001",
            npc_id="merchant_inkglass_dark",
            turn_type="quickchat",
            player_input="Turn in scene 2",
        ))

        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        pending = state.json()["pending_turns"]
        assert len(pending) == 2
        scene_ids = {p["scene_id"] for p in pending}
        assert scene1 in scene_ids
        assert scene2 in scene_ids
