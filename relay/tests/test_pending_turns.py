"""Tests for pending turn persistence and recovery.

Improvements (step 9):
 - (#1)  Stale turn timeout
 - (#2)  Client-initiated resume (turn_resume WebSocket message)
 - (#3)  Stage transition validation
 - (#4)  Retry count / attempt tracking
 - (#5)  Session-scoped recovery
 - (#6)  created_at in recovery payload
 - (#7)  character_snapshot populated
 - (#8)  check_results persisted on complete_turn
 - (#9)  Pytest integration tests for recovery
 - (#10) Idempotency guard on complete_turn
 - (#11) Non-blocking recovery (batch frame)
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from relay.tests.conftest import make_stub_npc


@pytest.fixture(autouse=True)
def _mock_load_npc():
    """Mock load_npc so scene creation doesn't require real NPC files."""
    with patch("relay.endpoints.scene.load_npc", return_value=make_stub_npc()):
        yield


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

        from relay.persistence.pending_turns import create_pending_turn

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Andalu looks around the room.",
                character_snapshot={"level": 6},
            )
        )
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
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turn,
            update_stage,
        )

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="She examines the vials.",
            )
        )

        # Progress through stages
        asyncio.run(update_stage(turn_id, "analysis"))
        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "analysis"

        asyncio.run(
            update_stage(
                turn_id,
                "checks_resolved",
                analysis_result={"checks": [{"skill": "perception", "dc": 14}]},
            )
        )
        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "checks_resolved"
        assert pt["analysis_result"]["checks"][0]["skill"] == "perception"

        asyncio.run(
            update_stage(
                turn_id,
                "streaming",
                check_results=[
                    {"skill": "perception", "dc": 14, "passed": True, "roll": 18, "modifier": 5, "total": 23}
                ],
            )
        )
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

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="She opens the cabinet.",
            )
        )

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

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Test input.",
            )
        )

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

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Andalu reaches for the book.",
            )
        )

        # Simulate crash after analysis stage
        asyncio.run(
            update_stage(
                turn_id,
                "analysis",
                analysis_result={"draft_response": "Seta frowns."},
            )
        )

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

        asyncio.run(
            create_pending_turn(
                scene_id=scene1,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Turn in scene 1",
            )
        )
        asyncio.run(
            create_pending_turn(
                scene_id=scene2,
                player_id="player_001",
                npc_id="merchant_inkglass_dark",
                turn_type="quickchat",
                player_input="Turn in scene 2",
            )
        )

        state = db_client.get(f"/session/{session_id}/state", headers=auth_header)
        pending = state.json()["pending_turns"]
        assert len(pending) == 2
        scene_ids = {p["scene_id"] for p in pending}
        assert scene1 in scene_ids
        assert scene2 in scene_ids


class TestStageTransitionValidation:
    """#3 — Forward-only stage transitions."""

    def test_forward_transition_allowed(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import create_pending_turn, get_pending_turn, update_stage

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Forward test.",
            )
        )
        asyncio.run(update_stage(turn_id, "analysis"))
        asyncio.run(update_stage(turn_id, "checks_resolved"))
        asyncio.run(update_stage(turn_id, "streaming"))

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "streaming"

    def test_backward_transition_rejected(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            StageTransitionError,
            create_pending_turn,
            update_stage,
        )

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Backward test.",
            )
        )
        asyncio.run(update_stage(turn_id, "checks_resolved"))

        with pytest.raises(StageTransitionError, match="Backward stage transition"):
            asyncio.run(update_stage(turn_id, "analysis"))

    def test_transition_from_complete_rejected(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            StageTransitionError,
            complete_turn,
            create_pending_turn,
            update_stage,
        )

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Complete test.",
            )
        )
        asyncio.run(complete_turn(turn_id, "Done."))

        with pytest.raises(StageTransitionError, match="terminal state 'complete'"):
            asyncio.run(update_stage(turn_id, "streaming"))

    def test_transition_to_failed_always_allowed(self, db_client, auth_header, session_and_scene):
        """Any non-terminal stage can transition to 'failed'."""
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import create_pending_turn, get_pending_turn, update_stage

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Fail test.",
            )
        )
        asyncio.run(update_stage(turn_id, "streaming"))
        asyncio.run(update_stage(turn_id, "failed", error_message="crash"))

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "failed"
        assert pt["error_message"] == "crash"


class TestIdempotencyGuard:
    """#10 — complete_turn is idempotent."""

    def test_double_complete_is_noop(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import complete_turn, create_pending_turn

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Idempotent test.",
            )
        )
        asyncio.run(complete_turn(turn_id, "First response."))
        # Second call should be a no-op (no scene_count increment)
        asyncio.run(complete_turn(turn_id, "Duplicate response."))

        scene = db_client.get(f"/scene/{scene_id}", headers=auth_header)
        assert scene.json()["turn_count"] == 1  # Only incremented once

    def test_complete_persists_check_results(self, db_client, auth_header, session_and_scene):
        """#8 — check_results saved on the completed turn record."""
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import complete_turn, create_pending_turn, get_pending_turn

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Check persist test.",
            )
        )
        checks = [{"skill": "stealth", "dc": 15, "passed": True, "roll": 18, "total": 23}]
        asyncio.run(complete_turn(turn_id, "Passed!", check_results=checks))

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "complete"
        assert pt["check_results"] == checks


class TestStaleTurnTimeout:
    """#1 — Stale turn cleanup."""

    def test_stale_turn_marked_failed(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turn,
            mark_stale_turns,
            update_stage,
        )

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Stale test.",
            )
        )
        asyncio.run(update_stage(turn_id, "analysis"))

        # Mark stale with threshold=0 (all turns are stale)
        count = asyncio.run(mark_stale_turns("player_001", threshold_seconds=0))
        assert count == 1

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "failed"
        assert "stale_timeout" in pt["error_message"]

    def test_fresh_turn_not_marked_stale(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turn,
            mark_stale_turns,
        )

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Fresh test.",
            )
        )

        # Threshold is very large — turn is fresh
        count = asyncio.run(mark_stale_turns("player_001", threshold_seconds=99999))
        assert count == 0

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "received"  # Unchanged

    def test_stale_turn_scoped_to_session(self, db_client, auth_header, character_id):
        """#5 — mark_stale_turns respects session_id filter."""
        from relay.persistence.pending_turns import (
            create_pending_turn,
            get_pending_turn,
            mark_stale_turns,
        )

        sess = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session_id = sess.json()["session_id"]
        scene = db_client.post(
            "/scene",
            json={"session_id": session_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        )
        scene_id = scene.json()["id"]

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Session scope test.",
            )
        )

        # Wrong session_id → nothing marked
        count = asyncio.run(mark_stale_turns("player_001", session_id="nonexistent_session", threshold_seconds=0))
        assert count == 0

        # Correct session_id → turn is marked
        count = asyncio.run(mark_stale_turns("player_001", session_id=session_id, threshold_seconds=0))
        assert count == 1

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["stage"] == "failed"


class TestRetryTracking:
    """#4 — Retry count and parent_turn_id."""

    def test_create_with_retry_fields(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import create_pending_turn, get_pending_turn

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Retry test.",
                parent_turn_id="pt_original123",
                retry_count=2,
            )
        )

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["retry_count"] == 2
        assert pt["parent_turn_id"] == "pt_original123"

    def test_default_retry_count_is_zero(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import create_pending_turn, get_pending_turn

        turn_id = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="No retry.",
            )
        )

        pt = asyncio.run(get_pending_turn(turn_id))
        assert pt["retry_count"] == 0
        assert pt["parent_turn_id"] is None

    def test_get_retry_count(self, db_client, auth_header, session_and_scene):
        _session_id, scene_id = session_and_scene

        from relay.persistence.pending_turns import (
            create_pending_turn,
            fail_turn,
            get_retry_count,
        )

        # Create and fail two turns with same input
        t1 = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Same input here.",
            )
        )
        asyncio.run(fail_turn(t1, "error1"))

        t2 = asyncio.run(
            create_pending_turn(
                scene_id=scene_id,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Same input here.",
            )
        )
        asyncio.run(fail_turn(t2, "error2"))

        count = asyncio.run(get_retry_count(scene_id, "Same input here."))
        assert count == 2


class TestSessionScopedRecovery:
    """#5 — get_pending_turns respects session_id filter."""

    def test_returns_only_current_session_turns(self, db_client, auth_header, character_id):
        from relay.persistence.pending_turns import create_pending_turn, get_pending_turns

        # Session 1
        sess1 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session1_id = sess1.json()["session_id"]
        scene1 = db_client.post(
            "/scene",
            json={"session_id": session1_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        ).json()["id"]

        asyncio.run(
            create_pending_turn(
                scene_id=scene1,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Session 1 turn.",
            )
        )

        # End session 1 to allow session 2
        db_client.post(f"/session/{session1_id}/end", json={}, headers=auth_header)

        # Session 2
        sess2 = db_client.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=auth_header,
        )
        session2_id = sess2.json()["session_id"]
        scene2 = db_client.post(
            "/scene",
            json={"session_id": session2_id, "npc_id": "seta_inkglass_dark", "mode": "rp"},
            headers=auth_header,
        ).json()["id"]

        asyncio.run(
            create_pending_turn(
                scene_id=scene2,
                player_id="player_001",
                npc_id="seta_inkglass_dark",
                turn_type="rp",
                player_input="Session 2 turn.",
            )
        )

        # Without session filter: gets all (only session2 since session1 turns were failed on end)
        _all_turns = asyncio.run(get_pending_turns("player_001"))

        # With session2 filter: gets only session 2
        session2_turns = asyncio.run(get_pending_turns("player_001", session_id=session2_id))
        assert len(session2_turns) == 1
        assert session2_turns[0]["player_input"] == "Session 2 turn."

        # With session1 filter: gets nothing (turns were failed on session end)
        session1_turns = asyncio.run(get_pending_turns("player_001", session_id=session1_id))
        assert len(session1_turns) == 0
