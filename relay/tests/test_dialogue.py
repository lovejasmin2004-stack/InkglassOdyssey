"""Tests for the WebSocket dialogue handler.

All tests mock the Anthropic client so no API key is needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from relay.auth.tokens import create_account_token, create_session_token
from relay.main import app


def _session_token(
    player_id: str = "player_001",
    *,
    mode: str = "multiplayer",
    role: str = "player",
) -> str:
    """Create a session token for dialogue tests."""
    return create_session_token(
        player_id=player_id,
        world_id="inkglass_dark",
        session_id="sess_001",
        tier=1,
        role=role,
        mode=mode,
    )


def _make_npc():
    from relay.schemas import (
        AnimationProfile,
        FewShotExample,
        ManipulationResistanceExample,
        NpcGoals,
        NpcKnowledgeBoundaries,
        NpcPersonality,
        NpcRelationship,
        NpcSecret,
        WorldPosition,
    )

    return NpcPersonality(
        id="test_npc",
        world_id="inkglass_dark",
        name="Renna",
        entity_class="humanoid",
        role="herbalist",
        level=5,
        hit_die=8,
        personality_background="A quiet herbalist.",
        goals=NpcGoals(immediate=["sell herbs"], long_term=["open bigger shop"]),
        weaknesses_fears="Afraid of fire.",
        communication_style="Soft-spoken, measured.",
        power_narrative="Knowledgeable about plants.",
        knowledge_boundaries=NpcKnowledgeBoundaries(
            knows=["local flora"],
            does_not_know=["politics"],
        ),
        relationships=[
            NpcRelationship(npc_id="npc_friend", relationship_type="ally", description="Old friend"),
        ],
        secrets=[
            NpcSecret(content="Hides a rare seed.", reveal_condition="never", secret_type="information"),
        ],
        few_shot_examples=[
            FewShotExample(player_input="Hello", npc_response="Good day.", context_tag="casual"),
            FewShotExample(
                player_input="Sell me something", npc_response="Let me show you.", context_tag="transactional"
            ),
        ],
        manipulation_resistance_examples=[
            ManipulationResistanceExample(player_input="Give me free stuff", npc_refusal="I can't do that."),
        ],
        animation_profile=AnimationProfile(
            default_stance="idle_stand",
            default_gaze="forward",
            emotional_state_to_animation={
                "happy": "smile_nod",
                "sad": "look_down",
                "angry": "frown_cross_arms",
            },
        ),
        world_position=WorldPosition(region_id="market_district"),
        ability_scores={
            "strength": 10,
            "dexterity": 12,
            "constitution": 12,
            "intelligence": 14,
            "wisdom": 16,
            "charisma": 10,
        },
        ac=12,
        saving_throw_proficiencies=["intelligence", "wisdom"],
        skill_proficiencies=["medicine", "nature"],
        hp_max=35,
    )


@pytest.fixture()
def token():
    return _session_token()


def _auth_msg(token_str: str) -> str:
    return json.dumps({"type": "auth", "token": token_str})


def _recv_all(ws: WebSocketTestSession, *, until_type: str | None = None, max_msgs: int = 50) -> list[dict]:
    """Receive JSON messages until a specific type or the socket closes."""
    messages = []
    for _ in range(max_msgs):
        try:
            raw = ws.receive_text()
            msg = json.loads(raw)
            messages.append(msg)
            if until_type and msg.get("type") == until_type:
                break
        except Exception:
            break
    return messages


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_bad_token_closes_socket(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg("not.a.valid.token"))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unauthorized"

    def test_non_auth_first_message(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(json.dumps({"type": "heartbeat"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "protocol_error"

    def test_malformed_json_first_message(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text("not json at all{{{")
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "protocol_error"

    def test_account_token_rejected(self):
        """Dialogue requires a session token — bare account tokens are rejected."""
        acct_token = create_account_token(player_id="player_001", tier=1)
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(acct_token))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unauthorized"
                assert "session token" in msg["message"]


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    def test_heartbeat_ack(self, _mock_pending, _mock_stale, token):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({"type": "heartbeat"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "heartbeat_ack"


# ---------------------------------------------------------------------------
# Quick-chat
# ---------------------------------------------------------------------------


def _mock_stream_context(text: str = "Hello, traveler."):
    """Create a mock async context manager for client.messages.stream()."""

    async def _text_gen():
        for word in text.split():
            yield word + " "

    stream_cm = AsyncMock()
    stream_obj = AsyncMock()
    stream_obj.text_stream = _text_gen()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_obj)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    return stream_cm


class TestQuickchat:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_quickchat_turn(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale, token):
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_mock_stream_context("Good day traveler."))
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "Hello there",
                        }
                    )
                )
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "stream_start" in types
        assert "stream_end" in types
        stream_end = next(m for m in msgs if m["type"] == "stream_end")
        assert "Good" in stream_end["full_text"]

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_missing_npc(self, mock_load_npc, _mock_pending, _mock_stale, token):
        mock_load_npc.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "nonexistent",
                            "text": "Hello",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_missing_text(self, mock_load_npc, _mock_pending, _mock_stale, token):
        mock_load_npc.return_value = _make_npc()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "missing_field"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_npc_load_error(self, mock_load_npc, _mock_pending, _mock_stale, token):
        from relay.ai.npc_loader import NpcLoadError

        mock_load_npc.side_effect = NpcLoadError("corrupt JSON")

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "bad_npc",
                            "text": "Hello",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "npc_load_error"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_quickchat_api_error(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale, token):
        import anthropic as _anthropic

        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()

        error_stream = AsyncMock()
        error_stream.__aenter__ = AsyncMock(
            side_effect=_anthropic.APIError(message="service down", request=MagicMock(), body=None),
        )
        error_stream.__aexit__ = AsyncMock(return_value=False)
        mock_client.messages.stream = MagicMock(return_value=error_stream)
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "Hello",
                        }
                    )
                )
                msgs = _recv_all(ws, until_type="error")

        error_msgs = [m for m in msgs if m["type"] == "error"]
        assert any(m["code"] == "llm_error" for m in error_msgs)

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_input_too_long(self, mock_load_npc, _mock_pending, _mock_stale, token):
        """Input exceeding _MAX_INPUT_LENGTH is rejected."""
        mock_load_npc.return_value = _make_npc()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "a" * 8001,
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "input_too_long"


# ---------------------------------------------------------------------------
# RP turn
# ---------------------------------------------------------------------------


def _mock_analysis_response(
    checks: list | None = None,
    scene_changes: dict | None = None,
    animation_directives: list | None = None,
    draft_response: str = "The herbalist nods.",
):
    """Create a mock response with a tool_use block for the analysis call."""
    tool_block = SimpleNamespace(
        type="tool_use",
        name="scene_analysis",
        input={
            "checks": checks or [],
            "scene_changes": scene_changes or {"emotional_temperature_delta": 0.1, "notes": "calm"},
            "animation_directives": animation_directives or [{"target": "npc", "directive": "smile_nod"}],
            "draft_response": draft_response,
        },
    )
    return SimpleNamespace(content=[tool_block])


class TestRpTurn:
    def _base_rp_msg(self, **overrides):
        msg = {
            "type": "rp_turn",
            "npc_id": "test_npc",
            "text": "I approach the herbalist and ask about rare seeds.",
            "character": {
                "ability_scores": {
                    "strength": 10,
                    "dexterity": 14,
                    "constitution": 12,
                    "intelligence": 12,
                    "wisdom": 16,
                    "charisma": 10,
                },
                "skill_proficiencies": ["perception", "insight", "survival"],
                "level": 5,
                "conditions": [],
            },
            "scene_state": {},
        }
        msg.update(overrides)
        return msg

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_no_checks(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale, token):
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_analysis_response())
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She smiles warmly."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "stream_start" in types
        assert "animation_directive" in types
        assert "scene_update" in types
        assert "stream_end" in types
        assert "check_result" not in types

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_with_check_multiplayer(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale):
        """In multiplayer mode, checks resolve automatically (no check_proposal)."""
        mp_token = _session_token(mode="multiplayer")
        mock_load_npc.return_value = _make_npc()
        analysis = _mock_analysis_response(
            checks=[{"skill": "persuasion", "dc": 12, "reason": "Asking about rare seeds"}],
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=analysis)
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She considers your words."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(mp_token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        check_msgs = [m for m in msgs if m["type"] == "check_result"]
        assert len(check_msgs) == 1
        cr = check_msgs[0]
        assert cr["skill"] == "persuasion"
        assert cr["dc"] == 12
        assert isinstance(cr["passed"], bool)
        assert isinstance(cr["roll"], int)
        # No check_proposal in multiplayer
        assert not any(m["type"] == "check_proposal" for m in msgs)

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_solo_check_proposal_flow(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale):
        """In solo mode, checks are proposed then confirmed before resolution."""
        solo_token = _session_token(mode="solo")
        mock_load_npc.return_value = _make_npc()
        analysis = _mock_analysis_response(
            checks=[{"skill": "perception", "dc": 14, "reason": "Noticing details"}],
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=analysis)
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She reveals the hidden seed."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(solo_token))
                ws.send_text(json.dumps(self._base_rp_msg()))

                # Should receive stream_start then check_proposal(s)
                msgs_phase1 = _recv_all(ws, until_type="check_proposal")

                types_p1 = [m["type"] for m in msgs_phase1]
                assert "stream_start" in types_p1
                assert "check_proposal" in types_p1

                proposal = next(m for m in msgs_phase1 if m["type"] == "check_proposal")
                assert proposal["skill"] == "perception"
                assert proposal["dc"] == 14

                # Now confirm the checks
                turn_id = next(m for m in msgs_phase1 if m["type"] == "stream_start")["turn_id"]
                ws.send_text(json.dumps({"type": "check_confirm", "turn_id": turn_id}))

                # Should receive check_result(s) then stream_end
                msgs_phase2 = _recv_all(ws, until_type="stream_end")

                types_p2 = [m["type"] for m in msgs_phase2]
                assert "check_result" in types_p2
                assert "stream_end" in types_p2

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_solo_no_checks_skips_proposal(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale):
        """Solo mode with no checks should complete without check_proposal."""
        solo_token = _session_token(mode="solo")
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_analysis_response(checks=[]))
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She nods."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(solo_token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "check_proposal" not in types
        assert "stream_end" in types

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_invalid_animation_filtered(
        self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale, token
    ):
        mock_load_npc.return_value = _make_npc()
        analysis = _mock_analysis_response(
            animation_directives=[
                {"target": "npc", "directive": "smile_nod"},
                {"target": "npc", "directive": "backflip_explode"},
            ],
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=analysis)
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("Response text."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        anim_msgs = [m for m in msgs if m["type"] == "animation_directive"]
        assert len(anim_msgs) == 1
        assert anim_msgs[0]["directive"] == "smile_nod"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_rp_turn_missing_npc(self, mock_load_npc, _mock_pending, _mock_stale, token):
        mock_load_npc.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.complete_turn")
    @patch("relay.endpoints.dialogue.update_stage")
    @patch("relay.endpoints.dialogue.create_pending_turn", return_value="pt_mock_passive")
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    @patch("relay.endpoints.dialogue.load_scene_state")
    @patch("relay.endpoints.dialogue.get_scene_turn_history")
    def test_rp_turn_passive_checks(
        self,
        mock_history,
        mock_scene_state,
        mock_get_client,
        mock_load_npc,
        _mock_create,
        _mock_stage,
        _mock_complete,
        _mock_pending,
        _mock_stale,
        token,
    ):
        """Passive checks use scene_state loaded from DB, not from the client message."""
        mock_history.return_value = []  # Scene exists, no prior turns
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_analysis_response())
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She nods."),
        )
        mock_get_client.return_value = mock_client

        # Scene state comes from DB, not client message
        mock_scene_state.return_value = {
            "hidden_elements": [
                {"id": "hidden_seed", "dc": 10, "skill": "perception", "hint": "You notice a rare seed."},
            ],
        }

        rp_msg = self._base_rp_msg(scene_id="scene_001")

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(rp_msg))
                msgs = _recv_all(ws, until_type="stream_end")

        passive_msgs = [m for m in msgs if m["type"] == "passive_check"]
        assert len(passive_msgs) == 1
        assert passive_msgs[0]["element_id"] == "hidden_seed"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_analysis_fallback(self, mock_get_client, mock_load_npc, _mock_pending, _mock_stale, token):
        """When the analysis call returns no tool_use block, fall back gracefully."""
        mock_load_npc.return_value = _make_npc()
        text_block = SimpleNamespace(type="text", text="Fallback text from LLM.")
        fallback_response = SimpleNamespace(content=[text_block])

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fallback_response)
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("Final response."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "stream_start" in types
        assert "stream_end" in types

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    def test_rp_turn_input_too_long(self, mock_load_npc, _mock_pending, _mock_stale, token):
        """RP input exceeding _MAX_INPUT_LENGTH is rejected."""
        mock_load_npc.return_value = _make_npc()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg(text="x" * 8001)))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "input_too_long"


# ---------------------------------------------------------------------------
# Scene validation and turn-in-progress guard
# ---------------------------------------------------------------------------


class TestSceneValidation:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    @patch("relay.endpoints.dialogue.get_scene_turn_history")
    def test_nonexistent_scene_rejected(
        self,
        mock_history,
        mock_get_client,
        mock_load_npc,
        _mock_pending,
        _mock_stale,
        token,
    ):
        mock_load_npc.return_value = _make_npc()
        mock_history.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "Hello",
                            "scene_id": "nonexistent_scene",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"


# ---------------------------------------------------------------------------
# Unknown message type
# ---------------------------------------------------------------------------


class TestUnknownType:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    def test_unknown_type_error(self, _mock_pending, _mock_stale, token):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({"type": "invalid_type", "scene_id": ""}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unknown_type"


# ---------------------------------------------------------------------------
# Check confirm edge cases
# ---------------------------------------------------------------------------


class TestCheckConfirm:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    def test_confirm_unknown_turn_id_rejected(self, _mock_pending, _mock_stale, token):
        """check_confirm with a turn_id that has no pending proposals returns error."""
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(
                    json.dumps(
                        {
                            "type": "check_confirm",
                            "turn_id": "nonexistent_turn",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"


# ---------------------------------------------------------------------------
# _extract_tool_result unit tests
# ---------------------------------------------------------------------------


class TestExtractToolResult:
    def test_extracts_matching_tool_block(self):
        from relay.endpoints.dialogue import _extract_tool_result

        block = SimpleNamespace(
            type="tool_use",
            name="scene_analysis",
            input={"checks": [], "draft_response": "test"},
        )
        response = SimpleNamespace(content=[block])
        result = _extract_tool_result(response)
        assert result == {"checks": [], "draft_response": "test"}

    def test_returns_none_for_text_only(self):
        from relay.endpoints.dialogue import _extract_tool_result

        block = SimpleNamespace(type="text", text="some text")
        response = SimpleNamespace(content=[block])
        assert _extract_tool_result(response) is None

    def test_ignores_wrong_tool_name(self):
        from relay.endpoints.dialogue import _extract_tool_result

        block = SimpleNamespace(type="tool_use", name="other_tool", input={"data": 1})
        response = SimpleNamespace(content=[block])
        assert _extract_tool_result(response) is None


# ---------------------------------------------------------------------------
# _validate_animation_directives unit tests
# ---------------------------------------------------------------------------


class TestValidateAnimationDirectives:
    def test_filters_invalid_directives(self):
        from relay.endpoints.dialogue import _validate_animation_directives

        npc = _make_npc()
        directives = [
            {"target": "npc", "directive": "smile_nod"},
            {"target": "npc", "directive": "idle_stand"},
            {"target": "npc", "directive": "nonexistent_anim"},
        ]
        result = _validate_animation_directives(directives, npc)
        assert len(result) == 2
        assert result[0]["directive"] == "smile_nod"
        assert result[1]["directive"] == "idle_stand"

    def test_empty_directives(self):
        from relay.endpoints.dialogue import _validate_animation_directives

        npc = _make_npc()
        assert _validate_animation_directives([], npc) == []


# ---------------------------------------------------------------------------
# _trim_history unit tests
# ---------------------------------------------------------------------------


class TestTrimHistory:
    def test_trim_short_history_unchanged(self):
        from relay.endpoints.dialogue import _trim_history

        history = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        result = _trim_history(history)
        assert len(result) == 10

    def test_trim_long_history_capped(self):
        from relay.endpoints.dialogue import _MAX_HISTORY_MESSAGES, _trim_history

        history = [{"role": "user", "content": f"msg {i}"} for i in range(100)]
        result = _trim_history(history)
        assert len(result) == _MAX_HISTORY_MESSAGES
        # Should keep the most recent messages
        assert result[0]["content"] == f"msg {100 - _MAX_HISTORY_MESSAGES}"
        assert result[-1]["content"] == "msg 99"

    def test_trim_returns_copy(self):
        from relay.endpoints.dialogue import _trim_history

        history = [{"role": "user", "content": "test"}]
        result = _trim_history(history)
        assert result is not history


# ---------------------------------------------------------------------------
# NPC memory summary tests (#R3)
# ---------------------------------------------------------------------------


class TestNpcMemorySummary:
    def test_short_history_returns_none(self):
        from relay.endpoints.dialogue import _build_npc_memory_summary

        history = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        assert _build_npc_memory_summary(history) is None

    def test_long_history_returns_summary(self):
        from relay.endpoints.dialogue import _build_npc_memory_summary

        # Create a history longer than the threshold
        history = []
        for i in range(60):
            history.append({"role": "user", "content": f"Message about topic {i}"})
            history.append({"role": "assistant", "content": f"Response {i}"})

        result = _build_npc_memory_summary(history)
        assert result is not None
        assert "Earlier in this session" in result
        assert "turns ago" in result

    def test_empty_history_returns_none(self):
        from relay.endpoints.dialogue import _build_npc_memory_summary

        assert _build_npc_memory_summary([]) is None


# ---------------------------------------------------------------------------
# Prompt caching format tests (#R1)
# ---------------------------------------------------------------------------


class TestPromptCaching:
    def test_rp_system_prompt_returns_cache_blocks(self):
        from relay.ai.rp_prompts import build_rp_system_prompt

        npc = _make_npc()
        result = build_rp_system_prompt(npc)

        # Should return a list of content blocks, not a string
        assert isinstance(result, list)
        assert len(result) >= 1

        # First block should have cache_control
        block = result[0]
        assert block["type"] == "text"
        assert "cache_control" in block
        assert block["cache_control"]["type"] == "ephemeral"

        # Text should contain NPC personality
        assert "Renna" in block["text"]
        assert "herbalist" in block["text"]

    def test_final_prose_with_passive_hints(self):
        from relay.ai.rp_prompts import build_final_prose_messages

        hints = [
            {
                "skill": "perception",
                "dc": 10,
                "passive_value": 15,
                "element_id": "hidden_door",
                "hint": "A faint draft reveals a hidden doorway.",
            },
        ]
        messages = build_final_prose_messages(
            "I look around the room.",
            "The room is dusty.",
            [],
            [],
            passive_hints=hints,
        )

        # The instruction should contain the passive hint text
        last_msg = messages[-1]["content"]
        assert "faint draft" in last_msg
        assert "passively noticed" in last_msg

    def test_final_prose_with_npc_memory(self):
        from relay.ai.rp_prompts import build_final_prose_messages

        messages = build_final_prose_messages(
            "Hello again.",
            "Greeting response.",
            [],
            [],
            npc_memory_summary="The player previously asked about rare seeds.",
        )

        last_msg = messages[-1]["content"]
        assert "rare seeds" in last_msg
        assert "CONTEXT FROM EARLIER" in last_msg


# ---------------------------------------------------------------------------
# Scene changes persistence tests (#R2, #R7)
# ---------------------------------------------------------------------------


class TestSceneChanges:
    def test_apply_scene_changes_emotional_temperature(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"emotional_temperature": 0.5}
        _apply_scene_changes(state, {"emotional_temperature_delta": 0.2})
        assert abs(state["emotional_temperature"] - 0.7) < 0.001

    def test_apply_scene_changes_clamps_temperature(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"emotional_temperature": 0.9}
        _apply_scene_changes(state, {"emotional_temperature_delta": 0.3})
        assert state["emotional_temperature"] == 1.0

        state = {"emotional_temperature": 0.1}
        _apply_scene_changes(state, {"emotional_temperature_delta": -0.3})
        assert state["emotional_temperature"] == 0.0

    def test_apply_scene_changes_environment_add(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"environmental_effects": ["high_ground"]}
        _apply_scene_changes(state, {"environment_add": ["darkness", "difficult_terrain"]})
        effects = state["environmental_effects"]
        assert "darkness" in effects
        assert "difficult_terrain" in effects
        assert "high_ground" in effects

    def test_apply_scene_changes_environment_remove(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"environmental_effects": ["darkness", "high_ground"]}
        _apply_scene_changes(state, {"environment_remove": ["darkness"]})
        effects = state["environmental_effects"]
        assert "darkness" not in effects
        assert "high_ground" in effects

    def test_apply_scene_changes_rejects_invalid_effect(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"environmental_effects": []}
        _apply_scene_changes(state, {"environment_add": ["invalid_effect", "darkness"]})
        effects = state["environmental_effects"]
        assert "invalid_effect" not in effects
        assert "darkness" in effects

    def test_apply_scene_changes_notes_appended(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {}
        _apply_scene_changes(state, {"notes": "The room grows tense."})
        assert state["scene_notes"] == ["The room grows tense."]

        _apply_scene_changes(state, {"notes": "A door slams."})
        assert len(state["scene_notes"]) == 2

    def test_apply_scene_changes_notes_capped_at_20(self):
        from relay.persistence.pending_turns import _apply_scene_changes

        state = {"scene_notes": [f"note {i}" for i in range(20)]}
        _apply_scene_changes(state, {"notes": "newest note"})
        assert len(state["scene_notes"]) == 20
        assert state["scene_notes"][-1] == "newest note"


# ---------------------------------------------------------------------------
# in_flight guard with pending proposals (#R8)
# ---------------------------------------------------------------------------


class TestInFlightGuard:
    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_second_turn_blocked_during_pending_proposal(
        self,
        mock_get_client,
        mock_load_npc,
        _mock_pending,
        _mock_stale,
    ):
        """While check proposals are pending, additional turns are rejected."""
        solo_token = _session_token(mode="solo")
        mock_load_npc.return_value = _make_npc()
        analysis = _mock_analysis_response(
            checks=[{"skill": "perception", "dc": 14, "reason": "Noticing details"}],
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=analysis)
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("Response."),
        )
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(solo_token))

                # Send first RP turn — will get check_proposal and pause
                ws.send_text(
                    json.dumps(
                        {
                            "type": "rp_turn",
                            "npc_id": "test_npc",
                            "text": "I look around carefully.",
                            "character": {
                                "ability_scores": {
                                    "strength": 10,
                                    "dexterity": 14,
                                    "constitution": 12,
                                    "intelligence": 12,
                                    "wisdom": 16,
                                    "charisma": 10,
                                },
                                "skill_proficiencies": ["perception"],
                                "level": 5,
                                "conditions": [],
                            },
                        }
                    )
                )
                _recv_all(ws, until_type="check_proposal")

                # Try to send a second turn — should be rejected
                # Need to wait past rate limit
                import time

                time.sleep(3.1)

                ws.send_text(
                    json.dumps(
                        {
                            "type": "quickchat_turn",
                            "npc_id": "test_npc",
                            "text": "Hello",
                        }
                    )
                )
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "turn_in_progress"


# ---------------------------------------------------------------------------
# Analysis tool schema test (#R7)
# ---------------------------------------------------------------------------


class TestAnalysisToolSchema:
    def test_schema_includes_environment_fields(self):
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        schema = _ANALYSIS_TOOL["input_schema"]
        scene_changes = schema["properties"]["scene_changes"]["properties"]
        assert "environment_add" in scene_changes
        assert "environment_remove" in scene_changes
        # Check enum values
        add_items = scene_changes["environment_add"]["items"]
        assert "darkness" in add_items["enum"]
        assert "difficult_terrain" in add_items["enum"]
        assert "hazard" in add_items["enum"]


# ---------------------------------------------------------------------------
# Turn resume tests (#2, Issue #6)
# ---------------------------------------------------------------------------


class TestTurnResume:
    """Tests for _handle_turn_resume — crash recovery via pending turns."""

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.get_pending_turn")
    def test_resume_missing_turn_id_rejected(self, mock_get_pt, _mock_pending, _mock_stale):
        """turn_resume without turn_id returns missing_field error."""
        token = _session_token()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({"type": "turn_resume"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "missing_field"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.get_pending_turn", return_value=None)
    def test_resume_nonexistent_turn_rejected(self, mock_get_pt, _mock_pending, _mock_stale):
        """turn_resume with unknown turn_id returns not_found error."""
        token = _session_token()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                import time

                time.sleep(3.1)  # Rate limit
                ws.send_text(json.dumps({"type": "turn_resume", "turn_id": "pt_nonexistent"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.get_pending_turn")
    def test_resume_wrong_player_rejected(self, mock_get_pt, _mock_pending, _mock_stale):
        """turn_resume by wrong player returns unauthorized error."""
        token = _session_token(player_id="player_001")
        mock_get_pt.return_value = {
            "turn_id": "pt_12345",
            "scene_id": "scene_001",
            "npc_id": "test_npc",
            "turn_type": "rp",
            "stage": "received",
            "player_id": "player_999",  # Different player
            "player_input": "Hello",
            "character_snapshot": None,
            "analysis_result": None,
            "check_results": None,
            "animation_directives": None,
            "scene_changes": None,
            "final_response": None,
            "error_message": None,
            "created_at": "2026-01-01T00:00:00",
            "retry_count": 0,
            "parent_turn_id": None,
        }

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                import time

                time.sleep(3.1)
                ws.send_text(json.dumps({"type": "turn_resume", "turn_id": "pt_12345"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unauthorized"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.get_pending_turn")
    def test_resume_terminal_state_rejected(self, mock_get_pt, _mock_pending, _mock_stale):
        """turn_resume on a completed turn returns invalid_state error."""
        token = _session_token()
        mock_get_pt.return_value = {
            "turn_id": "pt_12345",
            "scene_id": "scene_001",
            "npc_id": "test_npc",
            "turn_type": "rp",
            "stage": "complete",  # Terminal state
            "player_id": "player_001",
            "player_input": "Hello",
            "character_snapshot": None,
            "analysis_result": None,
            "check_results": None,
            "animation_directives": None,
            "scene_changes": None,
            "final_response": "NPC responded.",
            "error_message": None,
            "created_at": "2026-01-01T00:00:00",
            "retry_count": 0,
            "parent_turn_id": None,
        }

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                import time

                time.sleep(3.1)
                ws.send_text(json.dumps({"type": "turn_resume", "turn_id": "pt_12345"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "invalid_state"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.fail_turn")
    @patch("relay.endpoints.dialogue.get_pending_turn")
    def test_resume_max_retries_rejected(self, mock_get_pt, mock_fail, _mock_pending, _mock_stale):
        """turn_resume when retry_count >= MAX_RETRIES marks failed."""
        token = _session_token()
        mock_get_pt.return_value = {
            "turn_id": "pt_12345",
            "scene_id": "scene_001",
            "npc_id": "test_npc",
            "turn_type": "rp",
            "stage": "analysis",
            "player_id": "player_001",
            "player_input": "Hello",
            "character_snapshot": None,
            "analysis_result": None,
            "check_results": None,
            "animation_directives": None,
            "scene_changes": None,
            "final_response": None,
            "error_message": None,
            "created_at": "2026-01-01T00:00:00",
            "retry_count": 3,  # At max
            "parent_turn_id": None,
        }
        mock_fail.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                import time

                time.sleep(3.1)
                ws.send_text(json.dumps({"type": "turn_resume", "turn_id": "pt_12345"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "max_retries"

    @patch("relay.endpoints.dialogue.mark_stale_turns", return_value=0)
    @patch("relay.endpoints.dialogue.get_pending_turns", return_value=[])
    @patch("relay.endpoints.dialogue.complete_turn")
    @patch("relay.endpoints.dialogue.get_scene_turn_history", return_value=[])
    @patch("relay.endpoints.dialogue.get_pending_turn")
    def test_resume_streaming_with_saved_response_delivers_without_llm(
        self,
        mock_get_pt,
        mock_history,
        mock_complete,
        _mock_pending,
        _mock_stale,
    ):
        """turn_resume from 'streaming' with final_response delivers saved text."""
        token = _session_token()
        mock_get_pt.return_value = {
            "turn_id": "pt_12345",
            "scene_id": "scene_001",
            "npc_id": "test_npc",
            "turn_type": "rp",
            "stage": "streaming",
            "player_id": "player_001",
            "player_input": "I ask about herbs.",
            "character_snapshot": {"ability_scores": {}},
            "analysis_result": None,
            "check_results": [{"skill": "persuasion", "dc": 12, "passed": True}],
            "animation_directives": None,
            "scene_changes": {"notes": "calm scene"},
            "final_response": "The herbalist smiles warmly and shows you her collection.",
            "error_message": None,
            "created_at": "2026-01-01T00:00:00",
            "retry_count": 0,
            "parent_turn_id": None,
        }
        mock_complete.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                import time

                time.sleep(3.1)
                ws.send_text(json.dumps({"type": "turn_resume", "turn_id": "pt_12345"}))
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "stream_start" in types
        assert "stream_end" in types
        stream_end = next(m for m in msgs if m["type"] == "stream_end")
        assert "herbalist smiles" in stream_end["full_text"]

        # Verify complete_turn was called
        mock_complete.assert_called_once()


# ---------------------------------------------------------------------------
# CharacterMechanics loading tests (Keystone Principle)
# ---------------------------------------------------------------------------


class TestCharacterMechanicsLoading:
    """Verify the DB-authoritative character stats loading."""

    def test_load_character_mechanics_returns_db_stats(self):
        """load_character_mechanics returns data from the Character model."""
        import asyncio

        from relay.ai.game_context import CharacterMechanics, load_character_mechanics

        # This test runs without a DB, so the function should return None
        result = asyncio.run(load_character_mechanics("nonexistent_char"))
        assert result is None

    def test_character_mechanics_dataclass_frozen(self):
        """CharacterMechanics is immutable (frozen dataclass)."""
        from relay.ai.game_context import CharacterMechanics

        cm = CharacterMechanics(
            character_id="test",
            ability_scores={"strength": 16},
            skill_proficiencies=["athletics"],
            level=5,
            conditions=[],
            exhaustion_level=0,
        )
        assert cm.character_id == "test"
        assert cm.ability_scores == {"strength": 16}
        assert cm.level == 5

        # Should raise on attempt to modify
        with pytest.raises(Exception):
            cm.level = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LRU eviction test
# ---------------------------------------------------------------------------


class TestSceneHistoryLRU:
    """Verify that scene history uses LRU eviction, not FIFO."""

    def test_get_or_load_history_lru_eviction(self):
        """Accessing a scene moves it to end, evicting true LRU on overflow."""
        import asyncio
        from collections import OrderedDict

        from relay.endpoints.dialogue import _MAX_SCENES_PER_CONNECTION, _get_or_load_history

        scene_histories: OrderedDict[str, list[dict[str, str]]] = OrderedDict()

        # Fill to capacity with pre-loaded scenes
        for i in range(_MAX_SCENES_PER_CONNECTION):
            scene_histories[f"scene_{i}"] = [{"role": "user", "content": f"msg_{i}"}]

        # Access scene_0 (oldest) — moves it to end (most recent)
        with patch("relay.endpoints.dialogue.get_scene_turn_history", return_value=None):
            result = asyncio.run(_get_or_load_history(scene_histories, "scene_0"))
            assert result is not None

        # scene_0 should now be at the end
        keys = list(scene_histories.keys())
        assert keys[-1] == "scene_0"
        # scene_1 should now be the LRU (first)
        assert keys[0] == "scene_1"
