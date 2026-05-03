"""Tests for the WebSocket dialogue handler.

All tests mock the Anthropic client so no API key is needed.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.testclient import WebSocketTestSession

import relay.database as _db
from relay.ai.npc_loader import NpcLoadError, clear_cache
from relay.auth.tokens import create_account_token
from relay.database import get_db
from relay.main import app
from relay.middleware.rate_limit import clear_buckets
from relay.models import Base, Scene
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

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


def _make_npc() -> NpcPersonality:
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
            knows=["local flora"], does_not_know=["politics"],
        ),
        relationships=[
            NpcRelationship(npc_id="npc_friend", relationship_type="ally", description="Old friend"),
        ],
        secrets=[
            NpcSecret(content="Hides a rare seed.", reveal_condition="never", secret_type="information"),
        ],
        few_shot_examples=[
            FewShotExample(player_input="Hello", npc_response="Good day.", context_tag="casual"),
            FewShotExample(player_input="Sell me something", npc_response="Let me show you.", context_tag="transactional"),
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
        ability_scores={"strength": 10, "dexterity": 12, "constitution": 12, "intelligence": 14, "wisdom": 16, "charisma": 10},
        ac=12,
        saving_throw_proficiencies=["intelligence", "wisdom"],
        skill_proficiencies=["medicine", "nature"],
        hp_max=35,
    )


@pytest.fixture()
def _db_setup():
    """Set up in-memory DB for dialogue tests."""
    engine = create_async_engine(_TEST_DB_URL, future=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _drop_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    asyncio.run(_create_tables())

    original_factory = _db.AsyncSessionLocal
    _db.AsyncSessionLocal = session_factory
    app.dependency_overrides[get_db] = override_get_db
    clear_buckets()

    yield session_factory

    app.dependency_overrides.clear()
    _db.AsyncSessionLocal = original_factory
    asyncio.run(_drop_tables())
    asyncio.run(engine.dispose())


@pytest.fixture()
def token():
    return create_account_token(player_id="player_001", tier=1)


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
    def test_bad_token_closes_socket(self, _db_setup):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg("not.a.valid.token"))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unauthorized"

    def test_non_auth_first_message(self, _db_setup, token):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(json.dumps({"type": "heartbeat"}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "protocol_error"

    def test_malformed_json_first_message(self, _db_setup):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text("not json at all{{{")
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "protocol_error"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_heartbeat_ack(self, _db_setup, token):
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
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_quickchat_turn(self, mock_get_client, mock_load_npc, _db_setup, token):
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.stream = MagicMock(return_value=_mock_stream_context("Good day traveler."))
        mock_get_client.return_value = mock_client

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "test_npc",
                    "text": "Hello there",
                }))
                msgs = _recv_all(ws, until_type="stream_end")

        types = [m["type"] for m in msgs]
        assert "stream_start" in types
        assert "stream_end" in types
        stream_end = next(m for m in msgs if m["type"] == "stream_end")
        assert "Good" in stream_end["full_text"]

    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_missing_npc(self, mock_load_npc, _db_setup, token):
        mock_load_npc.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "nonexistent",
                    "text": "Hello",
                }))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"

    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_missing_text(self, mock_load_npc, _db_setup, token):
        mock_load_npc.return_value = _make_npc()

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "test_npc",
                    "text": "",
                }))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "missing_field"

    @patch("relay.endpoints.dialogue.load_npc")
    def test_quickchat_npc_load_error(self, mock_load_npc, _db_setup, token):
        mock_load_npc.side_effect = NpcLoadError("corrupt JSON")

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "bad_npc",
                    "text": "Hello",
                }))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "npc_load_error"

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_quickchat_api_error(self, mock_get_client, mock_load_npc, _db_setup, token):
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
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "test_npc",
                    "text": "Hello",
                }))
                msgs = _recv_all(ws, until_type="error")

        error_msgs = [m for m in msgs if m["type"] == "error"]
        assert any(m["code"] == "llm_error" for m in error_msgs)


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
                    "strength": 10, "dexterity": 14, "constitution": 12,
                    "intelligence": 12, "wisdom": 16, "charisma": 10,
                },
                "skill_proficiencies": ["perception", "insight", "survival"],
                "level": 5,
                "conditions": [],
            },
            "scene_state": {},
        }
        msg.update(overrides)
        return msg

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_no_checks(self, mock_get_client, mock_load_npc, _db_setup, token):
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

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_with_check(self, mock_get_client, mock_load_npc, _db_setup, token):
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
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msgs = _recv_all(ws, until_type="stream_end")

        check_msgs = [m for m in msgs if m["type"] == "check_result"]
        assert len(check_msgs) == 1
        cr = check_msgs[0]
        assert cr["skill"] == "persuasion"
        assert cr["dc"] == 12
        assert isinstance(cr["passed"], bool)
        assert isinstance(cr["roll"], int)

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_invalid_animation_filtered(self, mock_get_client, mock_load_npc, _db_setup, token):
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

    @patch("relay.endpoints.dialogue.load_npc")
    def test_rp_turn_missing_npc(self, mock_load_npc, _db_setup, token):
        mock_load_npc.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(self._base_rp_msg()))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_passive_checks(self, mock_get_client, mock_load_npc, _db_setup, token):
        mock_load_npc.return_value = _make_npc()
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_analysis_response())
        mock_client.messages.stream = MagicMock(
            return_value=_mock_stream_context("She nods."),
        )
        mock_get_client.return_value = mock_client

        rp_msg = self._base_rp_msg(scene_state={
            "hidden_elements": [
                {"id": "hidden_seed", "dc": 10, "skill": "perception", "hint": "You notice a rare seed."},
            ],
        })

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps(rp_msg))
                msgs = _recv_all(ws, until_type="stream_end")

        passive_msgs = [m for m in msgs if m["type"] == "passive_check"]
        assert len(passive_msgs) == 1
        assert passive_msgs[0]["element_id"] == "hidden_seed"

    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    def test_rp_turn_analysis_fallback(self, mock_get_client, mock_load_npc, _db_setup, token):
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


# ---------------------------------------------------------------------------
# Scene validation and turn-in-progress guard
# ---------------------------------------------------------------------------

class TestSceneValidation:
    @patch("relay.endpoints.dialogue.load_npc")
    @patch("relay.endpoints.dialogue._get_client")
    @patch("relay.endpoints.dialogue.get_scene_turn_history")
    def test_nonexistent_scene_rejected(
        self, mock_history, mock_get_client, mock_load_npc, _db_setup, token,
    ):
        mock_load_npc.return_value = _make_npc()
        mock_history.return_value = None

        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({
                    "type": "quickchat_turn",
                    "npc_id": "test_npc",
                    "text": "Hello",
                    "scene_id": "nonexistent_scene",
                }))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "not_found"


# ---------------------------------------------------------------------------
# Unknown message type
# ---------------------------------------------------------------------------

class TestUnknownType:
    def test_unknown_type_error(self, _db_setup, token):
        with TestClient(app, raise_server_exceptions=False) as client:
            with client.websocket_connect("/dialogue") as ws:
                ws.send_text(_auth_msg(token))
                ws.send_text(json.dumps({"type": "invalid_type", "scene_id": ""}))
                msg = json.loads(ws.receive_text())
                assert msg["type"] == "error"
                assert msg["code"] == "unknown_type"


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
