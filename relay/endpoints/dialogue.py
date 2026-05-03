"""WebSocket endpoint for NPC dialogue (quick-chat and RP mode)."""
from __future__ import annotations

import json
import logging
import time

import anthropic
import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from relay.ai.chat_prompts import build_quickchat_system_prompt
from relay.ai.npc_loader import load_npc
from relay.ai.rp_prompts import (
    build_analysis_messages,
    build_final_prose_messages,
    build_rp_system_prompt,
)
from relay.auth.tokens import decode_token
from relay.checks.resolver import resolve_check, validate_check
from relay.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dialogue"])

_RATE_LIMIT_SECONDS = 3.0
_MODEL = "claude-sonnet-4-6"


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _send_error(ws: WebSocket, code: str, message: str) -> None:
    await ws.send_json({"type": "error", "code": code, "message": message})


async def _authenticate(ws: WebSocket) -> dict | None:
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

    auth = await _authenticate(ws)
    if auth is None:
        await ws.close(code=1008)
        return

    player_id = auth["player_id"]
    logger.info("WS authenticated", extra={"player_id": player_id})

    history: list[dict[str, str]] = []
    last_message_time: float = 0.0
    in_flight = False

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "heartbeat":
                await ws.send_json({"type": "heartbeat_ack"})
                continue

            now = time.time()
            if now - last_message_time < _RATE_LIMIT_SECONDS:
                await _send_error(ws, "rate_limited", "Please wait before sending another message")
                continue
            last_message_time = now

            if in_flight:
                await _send_error(ws, "turn_in_progress", "A turn is already being processed")
                continue

            in_flight = True
            try:
                if msg_type == "quickchat_turn":
                    await _handle_quickchat(ws, msg, history)
                elif msg_type == "rp_turn":
                    await _handle_rp_turn(ws, msg, history)
                else:
                    await _send_error(ws, "unknown_type", f"Unrecognised message type: {msg_type}")
            finally:
                in_flight = False

    except WebSocketDisconnect:
        logger.info("WS disconnected", extra={"player_id": player_id})
    except Exception:
        logger.exception("WS error", extra={"player_id": player_id})
        try:
            await _send_error(ws, "internal_error", "An unexpected error occurred")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Quick-chat handler (single LLM call, no scene state)
# ---------------------------------------------------------------------------

async def _handle_quickchat(ws: WebSocket, msg: dict, history: list[dict[str, str]]) -> None:
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

    client = _get_client()
    turn_id = f"qc_{int(time.time() * 1000)}"
    await ws.send_json({"type": "stream_start", "turn_id": turn_id, "npc_id": npc_id})

    full_response = ""
    try:
        async with client.messages.stream(
            model=_MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=list(history),
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                await ws.send_json({"type": "stream_chunk", "turn_id": turn_id, "text": text})

        history.append({"role": "assistant", "content": full_response})
        await ws.send_json({"type": "stream_end", "turn_id": turn_id, "npc_id": npc_id, "full_text": full_response})
        logger.info("Quick-chat turn complete", extra={"turn_id": turn_id, "npc_id": npc_id})

    except anthropic.APIError as e:
        logger.error("Anthropic API error", extra={"error": str(e)})
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


# ---------------------------------------------------------------------------
# RP mode handler (two-call turn flow)
# ---------------------------------------------------------------------------

async def _handle_rp_turn(ws: WebSocket, msg: dict, history: list[dict[str, str]]) -> None:
    """Full RP turn: analysis call → check resolution → final prose call."""
    npc_id = msg.get("npc_id")
    player_prose = msg.get("text", "").strip()
    character_sheet = msg.get("character", {})

    if not npc_id:
        await _send_error(ws, "missing_field", "rp_turn requires 'npc_id'")
        return
    if not player_prose:
        await _send_error(ws, "missing_field", "rp_turn requires 'text'")
        return

    npc = load_npc(npc_id)
    if npc is None:
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    ability_scores = character_sheet.get("ability_scores", {})
    skill_profs = character_sheet.get("skill_proficiencies", [])
    level = character_sheet.get("level", 1)

    client = _get_client()
    turn_id = f"rp_{int(time.time() * 1000)}"
    system_prompt = build_rp_system_prompt(npc)

    await ws.send_json({"type": "stream_start", "turn_id": turn_id, "npc_id": npc_id})

    try:
        # === CALL 1: Structured analysis ===
        analysis_messages = build_analysis_messages(player_prose, history)

        analysis_response = await client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=analysis_messages,
        )

        analysis_text = analysis_response.content[0].text
        logger.debug("Analysis response received", extra={"turn_id": turn_id})

        analysis = _parse_analysis(analysis_text)
        if analysis is None:
            logger.warning("Failed to parse analysis JSON, falling back to draft-only", extra={"turn_id": turn_id})
            analysis = {"checks": [], "scene_changes": {}, "animation_directives": [], "draft_response": analysis_text}

        # === Validate and resolve checks ===
        raw_checks = analysis.get("checks", [])
        validated_checks = [validate_check(c) for c in raw_checks]
        check_results = []

        for vc in validated_checks:
            result = resolve_check(vc, ability_scores, skill_profs, level)
            check_results.append(result)
            await ws.send_json({
                "type": "check_result",
                "turn_id": turn_id,
                "skill": result["skill"],
                "dc": result["dc"],
                "roll": result["roll"],
                "modifier": result["modifier"],
                "total": result["total"],
                "passed": result["passed"],
                "reason": result["reason"],
            })

        # === Send animation directives ===
        valid_animations = _validate_animation_directives(
            analysis.get("animation_directives", []),
            npc,
        )
        for anim in valid_animations:
            await ws.send_json({
                "type": "animation_directive",
                "turn_id": turn_id,
                "target": anim["target"],
                "directive": anim["directive"],
            })

        # === Send scene update ===
        scene_changes = analysis.get("scene_changes", {})
        if scene_changes:
            await ws.send_json({
                "type": "scene_update",
                "turn_id": turn_id,
                "changes": scene_changes,
            })

        # === CALL 2: Final prose with check results ===
        draft = analysis.get("draft_response", "")
        final_messages = build_final_prose_messages(player_prose, draft, check_results, history)

        full_response = ""
        async with client.messages.stream(
            model=_MODEL,
            max_tokens=800,
            system=system_prompt,
            messages=final_messages,
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                await ws.send_json({"type": "stream_chunk", "turn_id": turn_id, "text": text})

        # === Commit to history ===
        history.append({"role": "user", "content": player_prose})
        history.append({"role": "assistant", "content": full_response})

        await ws.send_json({"type": "stream_end", "turn_id": turn_id, "npc_id": npc_id, "full_text": full_response})
        logger.info(
            "RP turn complete",
            extra={"turn_id": turn_id, "npc_id": npc_id, "checks_resolved": len(check_results)},
        )

    except anthropic.APIError as e:
        logger.error("Anthropic API error during RP turn", extra={"error": str(e), "turn_id": turn_id})
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


def _parse_analysis(text: str) -> dict | None:
    """Extract the JSON object from the analysis LLM response."""
    # The LLM may wrap JSON in markdown fences.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last ``` lines
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        cleaned = "\n".join(lines[start:end])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object within the text
        brace_start = cleaned.find("{")
        brace_end = cleaned.rfind("}")
        if brace_start != -1 and brace_end != -1:
            try:
                return json.loads(cleaned[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass
    return None


def _validate_animation_directives(
    directives: list[dict],
    npc,
) -> list[dict]:
    """Invariant #11: animation directives relay-validated before Unity receives them."""
    valid_anims = set(npc.animation_profile.emotional_state_to_animation.values())
    valid_anims.add(npc.animation_profile.default_stance)
    valid_anims.add(npc.animation_profile.default_gaze)

    validated = []
    for d in directives:
        directive = d.get("directive", "")
        if directive in valid_anims:
            validated.append(d)
        else:
            logger.debug(
                "Animation directive rejected — not in NPC profile",
                extra={"directive": directive, "npc_id": npc.id},
            )
    return validated
