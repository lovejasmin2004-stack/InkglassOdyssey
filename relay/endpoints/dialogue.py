"""WebSocket endpoint for NPC dialogue (quick-chat and RP mode)."""
from __future__ import annotations

import json
import logging
import time

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from relay.ai.chat_prompts import build_quickchat_system_prompt
from relay.ai.npc_loader import load_npc
from relay.auth.tokens import decode_token
from relay.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dialogue"])

# Per-session rate limit: min seconds between messages.
_RATE_LIMIT_SECONDS = 3.0


async def _send_error(ws: WebSocket, code: str, message: str) -> None:
    await ws.send_json({"type": "error", "code": code, "message": message})


async def _authenticate(ws: WebSocket) -> dict | None:
    """Expect the first message to be an auth frame. Returns decoded token or None."""
    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            await _send_error(ws, "protocol_error", "First message must be type 'auth'")
            return None
        token_str = msg.get("token", "")
        payload = decode_token(token_str)
        return payload.model_dump(mode="json")
    except jwt.PyJWTError:
        await _send_error(ws, "unauthorized", "Invalid or expired token")
        return None
    except Exception:
        await _send_error(ws, "protocol_error", "Malformed auth message")
        return None


@router.websocket("/dialogue")
async def dialogue_ws(ws: WebSocket) -> None:
    await ws.accept()

    # --- authenticate ---
    auth = await _authenticate(ws)
    if auth is None:
        await ws.close(code=1008)
        return

    player_id = auth["player_id"]
    logger.info("WS authenticated", extra={"player_id": player_id})

    # Conversation history kept in memory for the duration of this connection.
    history: list[dict[str, str]] = []
    last_message_time: float = 0.0
    in_flight = False

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            # --- heartbeat ---
            if msg_type == "heartbeat":
                await ws.send_json({"type": "heartbeat_ack"})
                continue

            # --- rate limit ---
            now = time.time()
            if now - last_message_time < _RATE_LIMIT_SECONDS:
                await _send_error(ws, "rate_limited", "Please wait before sending another message")
                continue
            last_message_time = now

            # --- turn in progress guard ---
            if in_flight:
                await _send_error(ws, "turn_in_progress", "A turn is already being processed")
                continue

            # --- quickchat_turn ---
            if msg_type == "quickchat_turn":
                in_flight = True
                try:
                    await _handle_quickchat(ws, msg, history)
                finally:
                    in_flight = False
            else:
                await _send_error(ws, "unknown_type", f"Unrecognised message type: {msg_type}")

    except WebSocketDisconnect:
        logger.info("WS disconnected", extra={"player_id": player_id})
    except Exception:
        logger.exception("WS error", extra={"player_id": player_id})
        try:
            await _send_error(ws, "internal_error", "An unexpected error occurred")
        except Exception:
            pass


async def _handle_quickchat(ws: WebSocket, msg: dict, history: list[dict[str, str]]) -> None:
    """Handle a quickchat_turn message: load NPC, call Anthropic, stream response."""
    npc_id = msg.get("npc_id")
    player_input = msg.get("text", "").strip()

    if not npc_id:
        await _send_error(ws, "missing_field", "quickchat_turn requires 'npc_id'")
        return
    if not player_input:
        await _send_error(ws, "missing_field", "quickchat_turn requires 'text'")
        return

    npc = load_npc(npc_id)
    if npc is None:
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    system_prompt = build_quickchat_system_prompt(npc)
    history.append({"role": "user", "content": player_input})

    # --- stream from Anthropic ---
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    turn_id = f"qc_{int(time.time() * 1000)}"
    await ws.send_json({"type": "stream_start", "turn_id": turn_id, "npc_id": npc_id})

    full_response = ""
    try:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=system_prompt,
            messages=list(history),
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                await ws.send_json({"type": "stream_chunk", "turn_id": turn_id, "text": text})

        history.append({"role": "assistant", "content": full_response})

        await ws.send_json({
            "type": "stream_end",
            "turn_id": turn_id,
            "npc_id": npc_id,
            "full_text": full_response,
        })
        logger.info("Quick-chat turn complete", extra={"turn_id": turn_id, "npc_id": npc_id})

    except anthropic.APIError as e:
        logger.error("Anthropic API error", extra={"error": str(e)})
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")
