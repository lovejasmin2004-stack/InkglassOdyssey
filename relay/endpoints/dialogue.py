"""WebSocket endpoint for NPC dialogue (quick-chat and RP mode)."""
from __future__ import annotations

import json
import logging
import time

import anthropic
import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from relay.ai.chat_prompts import build_quickchat_system_prompt
from relay.ai.npc_loader import NpcLoadError, load_npc
from relay.ai.rp_prompts import (
    build_analysis_messages,
    build_final_prose_messages,
    build_rp_system_prompt,
)
from relay.auth.tokens import decode_token
from relay.checks.resolver import evaluate_passive_checks, resolve_check, validate_check
from relay.config import settings
from relay.persistence.pending_turns import (
    complete_turn,
    create_pending_turn,
    fail_turn,
    get_pending_turns,
    get_scene_turn_history,
    update_stage,
)

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


async def _send_recovery_data(ws: WebSocket, player_id: str) -> None:
    """On reconnect, send any incomplete pending turns so the client can recover."""
    pending = await get_pending_turns(player_id)
    if not pending:
        return

    for pt in pending:
        await ws.send_json({
            "type": "turn_recovery",
            "turn_id": pt["turn_id"],
            "scene_id": pt["scene_id"],
            "npc_id": pt["npc_id"],
            "turn_type": pt["turn_type"],
            "stage": pt["stage"],
            "player_input": pt["player_input"],
            "check_results": pt["check_results"],
            "animation_directives": pt["animation_directives"],
            "scene_changes": pt["scene_changes"],
            "final_response": pt["final_response"],
        })

    logger.info(
        "Recovery data sent",
        extra={"player_id": player_id, "pending_count": len(pending)},
    )


@router.websocket("/dialogue")
async def dialogue_ws(ws: WebSocket) -> None:
    await ws.accept()

    auth = await _authenticate(ws)
    if auth is None:
        await ws.close(code=1008)
        return

    player_id = auth["player_id"]
    logger.info("WS authenticated", extra={"player_id": player_id})

    # Send recovery data for any interrupted turns
    await _send_recovery_data(ws, player_id)

    scene_histories: dict[str, list[dict[str, str]]] = {}
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

            scene_id = msg.get("scene_id", "")
            history = await _get_or_load_history(scene_histories, scene_id)
            if history is None:
                await _send_error(ws, "not_found", f"Scene '{scene_id}' not found")
                continue

            in_flight = True
            try:
                if msg_type == "quickchat_turn":
                    await _handle_quickchat(ws, msg, history, player_id)
                elif msg_type == "rp_turn":
                    await _handle_rp_turn(ws, msg, history, player_id)
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


async def _get_or_load_history(
    scene_histories: dict[str, list[dict[str, str]]],
    scene_id: str,
) -> list[dict[str, str]] | None:
    """Return the history list for a scene, loading from DB on first access.

    Returns None if *scene_id* is non-empty but does not match any scene
    in the database — callers should reject the turn.
    """
    if scene_id in scene_histories:
        return scene_histories[scene_id]

    if scene_id:
        history = await get_scene_turn_history(scene_id)
        if history is None:
            return None
        logger.info(
            "Scene history loaded from DB",
            extra={"scene_id": scene_id, "turns_restored": len(history) // 2},
        )
    else:
        history = []

    scene_histories[scene_id] = history
    return history


# ---------------------------------------------------------------------------
# Quick-chat handler (single LLM call, no scene state)
# ---------------------------------------------------------------------------

async def _handle_quickchat(
    ws: WebSocket, msg: dict, history: list[dict[str, str]], player_id: str,
) -> None:
    npc_id = msg.get("npc_id")
    player_input = msg.get("text", "").strip()
    scene_id = msg.get("scene_id", "")

    if not npc_id:
        await _send_error(ws, "missing_field", "quickchat_turn requires 'npc_id'")
        return
    if not player_input:
        await _send_error(ws, "missing_field", "quickchat_turn requires 'text'")
        return

    try:
        npc = load_npc(npc_id)
    except NpcLoadError as exc:
        logger.error("NPC file corrupt", extra={"npc_id": npc_id, "error": str(exc)})
        await _send_error(ws, "npc_load_error", f"NPC '{npc_id}' exists but failed to load: {exc}")
        return
    if npc is None:
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    # === Persist pending turn before any processing (Invariant #12) ===
    turn_id = ""
    if scene_id:
        turn_id = await create_pending_turn(
            scene_id=scene_id,
            player_id=player_id,
            npc_id=npc_id,
            turn_type="quickchat",
            player_input=player_input,
        )

    system_prompt = build_quickchat_system_prompt(npc)
    history.append({"role": "user", "content": player_input})

    client = _get_client()
    ws_turn_id = turn_id or f"qc_{int(time.time() * 1000)}"
    await ws.send_json({"type": "stream_start", "turn_id": ws_turn_id, "npc_id": npc_id})

    if turn_id:
        await update_stage(turn_id, "streaming")

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
                await ws.send_json({"type": "stream_chunk", "turn_id": ws_turn_id, "text": text})

        history.append({"role": "assistant", "content": full_response})
        await ws.send_json({
            "type": "stream_end", "turn_id": ws_turn_id,
            "npc_id": npc_id, "full_text": full_response,
        })

        if turn_id:
            await complete_turn(turn_id, full_response)

        logger.info("Quick-chat turn complete", extra={"turn_id": ws_turn_id, "npc_id": npc_id})

    except anthropic.APIError as e:
        logger.error("Anthropic API error", extra={"error": str(e)})
        if turn_id:
            await fail_turn(turn_id, str(e))
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


# ---------------------------------------------------------------------------
# RP mode handler (two-call turn flow)
# ---------------------------------------------------------------------------

async def _handle_rp_turn(
    ws: WebSocket, msg: dict, history: list[dict[str, str]], player_id: str,
) -> None:
    """Full RP turn: analysis call -> check resolution -> final prose call."""
    npc_id = msg.get("npc_id")
    player_prose = msg.get("text", "").strip()
    character_sheet = msg.get("character", {})
    scene_id = msg.get("scene_id", "")

    if not npc_id:
        await _send_error(ws, "missing_field", "rp_turn requires 'npc_id'")
        return
    if not player_prose:
        await _send_error(ws, "missing_field", "rp_turn requires 'text'")
        return

    try:
        npc = load_npc(npc_id)
    except NpcLoadError as exc:
        logger.error("NPC file corrupt", extra={"npc_id": npc_id, "error": str(exc)})
        await _send_error(ws, "npc_load_error", f"NPC '{npc_id}' exists but failed to load: {exc}")
        return
    if npc is None:
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    ability_scores = character_sheet.get("ability_scores", {})
    skill_profs = character_sheet.get("skill_proficiencies", [])
    level = character_sheet.get("level", 1)
    conditions = character_sheet.get("conditions", [])
    scene_state = msg.get("scene_state", {})
    environmental_effects = scene_state.get("environmental_effects", [])

    # === Persist pending turn before any processing (Invariant #12) ===
    turn_id = ""
    if scene_id:
        turn_id = await create_pending_turn(
            scene_id=scene_id,
            player_id=player_id,
            npc_id=npc_id,
            turn_type="rp",
            player_input=player_prose,
            character_snapshot=character_sheet,
        )

    client = _get_client()
    ws_turn_id = turn_id or f"rp_{int(time.time() * 1000)}"
    system_prompt = build_rp_system_prompt(npc)

    await ws.send_json({"type": "stream_start", "turn_id": ws_turn_id, "npc_id": npc_id})

    try:
        # === CALL 1: Structured analysis ===
        if turn_id:
            await update_stage(turn_id, "analysis")

        analysis_messages = build_analysis_messages(player_prose, history)

        analysis_response = await client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=analysis_messages,
            tools=[_ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "scene_analysis"},
        )

        analysis = _extract_tool_result(analysis_response)
        if analysis is None:
            logger.warning(
                "Analysis tool call not returned, falling back to draft-only",
                extra={"turn_id": ws_turn_id},
            )
            fallback_text = ""
            for block in analysis_response.content:
                if hasattr(block, "text"):
                    fallback_text = block.text
                    break
            analysis = {
                "checks": [], "scene_changes": {},
                "animation_directives": [], "draft_response": fallback_text,
            }
        logger.debug("Analysis response received", extra={"turn_id": ws_turn_id})

        if turn_id:
            await update_stage(turn_id, "checks_resolved", analysis_result=analysis)

        # === Validate and resolve checks ===
        raw_checks = analysis.get("checks", [])
        validated_checks = [validate_check(c) for c in raw_checks]
        check_results = []

        for vc in validated_checks:
            result = resolve_check(
                vc, ability_scores, skill_profs, level,
                conditions=conditions,
                environmental_effects=environmental_effects,
            )
            check_results.append(result)
            await ws.send_json({
                "type": "check_result",
                "turn_id": ws_turn_id,
                "skill": result["skill"],
                "dc": result["dc"],
                "roll": result["roll"],
                "dice": result["dice"],
                "roll_mode": result["roll_mode"],
                "modifier": result["modifier"],
                "total": result["total"],
                "passed": result["passed"],
                "reason": result["reason"],
            })

        # === Evaluate passive checks (Invariant #22) ===
        passive_hints = evaluate_passive_checks(
            ability_scores, skill_profs, level, scene_state,
            conditions=conditions,
        )
        for hint in passive_hints:
            await ws.send_json({
                "type": "passive_check",
                "turn_id": ws_turn_id,
                "skill": hint["skill"],
                "dc": hint["dc"],
                "passive_value": hint["passive_value"],
                "element_id": hint["element_id"],
            })

        # === Send animation directives ===
        valid_animations = _validate_animation_directives(
            analysis.get("animation_directives", []),
            npc,
        )
        for anim in valid_animations:
            await ws.send_json({
                "type": "animation_directive",
                "turn_id": ws_turn_id,
                "target": anim["target"],
                "directive": anim["directive"],
            })

        # === Send scene update ===
        scene_changes = analysis.get("scene_changes", {})
        if scene_changes:
            await ws.send_json({
                "type": "scene_update",
                "turn_id": ws_turn_id,
                "changes": scene_changes,
            })

        if turn_id:
            await update_stage(
                turn_id, "streaming",
                check_results=check_results,
                animation_directives=valid_animations,
                scene_changes=scene_changes,
            )

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
                await ws.send_json({"type": "stream_chunk", "turn_id": ws_turn_id, "text": text})

        # === Commit to history ===
        history.append({"role": "user", "content": player_prose})
        history.append({"role": "assistant", "content": full_response})

        await ws.send_json({
            "type": "stream_end", "turn_id": ws_turn_id,
            "npc_id": npc_id, "full_text": full_response,
        })

        if turn_id:
            await complete_turn(turn_id, full_response)

        logger.info(
            "RP turn complete",
            extra={"turn_id": ws_turn_id, "npc_id": npc_id, "checks_resolved": len(check_results)},
        )

    except anthropic.APIError as e:
        logger.error("Anthropic API error during RP turn", extra={"error": str(e), "turn_id": ws_turn_id})
        if turn_id:
            await fail_turn(turn_id, str(e))
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


_ANALYSIS_TOOL: dict = {
    "name": "scene_analysis",
    "description": "Return the structured analysis of a player's RP turn.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "dc": {"type": "integer", "minimum": 5, "maximum": 30},
                        "reason": {"type": "string"},
                        "advantage": {"type": "boolean"},
                        "disadvantage": {"type": "boolean"},
                    },
                    "required": ["skill", "dc", "reason"],
                },
            },
            "scene_changes": {
                "type": "object",
                "properties": {
                    "emotional_temperature_delta": {"type": "number"},
                    "notes": {"type": "string"},
                },
            },
            "animation_directives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "directive": {"type": "string"},
                    },
                    "required": ["target", "directive"],
                },
            },
            "draft_response": {"type": "string"},
        },
        "required": ["checks", "scene_changes", "animation_directives", "draft_response"],
    },
}


def _extract_tool_result(response) -> dict | None:
    """Extract the tool-use input from the analysis LLM response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "scene_analysis":
            return block.input
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
                "Animation directive rejected -- not in NPC profile",
                extra={"directive": directive, "npc_id": npc.id},
            )
    return validated
