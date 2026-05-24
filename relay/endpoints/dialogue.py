"""WebSocket endpoint for NPC dialogue (quick-chat and RP mode).

Improvements (step 6 — quick-chat):
 - (#1)  Solo check_confirm / check_proposal flow
 - (#2)  Input length validation (max 8000 chars)
 - (#3)  History sliding window (last 40 messages sent to LLM)
 - (#6)  Session token validation (require session context for dialogue)
 - (#7)  RP history appended before LLM call for resilience
 - (#8)  Max concurrent scene histories per connection (cap at 10)
 - (#9)  Single Anthropic client per WebSocket connection
 - (#10) Graceful ws.close(1011) on fatal error
 - (#11) Scene state loaded from DB, not trusted from client

Improvements (step 7 — RP mode):
 - (#R1)  Anthropic prompt caching on system prompt (Tier 1)
 - (#R2)  Scene changes committed to DB after each turn
 - (#R3)  NPC memory summary for trimmed history continuity
 - (#R4)  Per-turn analytics tracking (timing, LLM calls, cache hits)
 - (#R5)  load_npc uses session world_id
 - (#R6)  Atomic commit (pending turn + scene state in one transaction)
 - (#R7)  Expanded scene_changes schema (environment_add/remove)
 - (#R8)  in_flight guard covers solo check_confirm pause
 - (#R9)  check_confirm validates player ownership
 - (#R10) Passive check hints injected into final prose call
 - (#R11) Full NPC response stored in turn history (no truncation)
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict

import anthropic
import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

import relay.database as _db
from relay.ai.chat_prompts import build_quickchat_system_prompt
from relay.ai.game_context import (
    CharacterMechanics,
    GameStateContext,
    load_character_mechanics,
    load_game_context,
    resolve_character_id,
)
from relay.ai.npc_loader import NpcLoadError, load_npc
from relay.ai.rp_prompts import (
    build_analysis_messages,
    build_final_prose_messages,
    build_rp_system_prompt,
)
from relay.auth.tokens import SessionTokenPayload, decode_token
from relay.checks.resolver import evaluate_passive_checks, resolve_check, validate_checks_batch
from relay.config import settings
from relay.consequences.evaluator import apply_world_mutations, validate_world_mutations
from relay.consequences.profiles import filter_mutations_by_profile, resolve_profile
from relay.models import Character
from relay.narrative.threads import upsert_thread, validate_narrative_signals
from relay.persistence.pending_turns import (
    MAX_RETRIES,
    complete_turn,
    create_pending_turn,
    fail_turn,
    get_pending_turn,
    get_pending_turns,
    get_scene_turn_history,
    load_scene_state,
    mark_stale_turns,
    update_stage,
)
from relay.world.content_loader import load_faction_registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dialogue"])

_RATE_LIMIT_SECONDS = 3.0
_MODEL = "claude-sonnet-4-6"

# Maximum length of player text input (characters).
_MAX_INPUT_LENGTH = 8000

# Maximum number of history messages passed to the LLM (sliding window).
_MAX_HISTORY_MESSAGES = 40

# Maximum concurrent scene histories held in memory per connection.
_MAX_SCENES_PER_CONNECTION = 10

# When history exceeds this threshold, generate a memory summary (#R3).
_MEMORY_SUMMARY_THRESHOLD = 30

# Hard cap on automatic AI continuation chains (Phase 4 multi-NPC, Phase 2 director).
# Lower than ai-gamemaster's 20 — streaming costs more per step.
_MAX_AI_CHAIN_DEPTH = 10

# Timeout for LLM API calls in seconds. Prevents indefinite hangs if Anthropic
# API is unresponsive. The pending turn becomes stale after 5 min regardless,
# but this releases the WebSocket sooner for a better player experience.
_LLM_TIMEOUT_SECONDS = 45.0


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=_LLM_TIMEOUT_SECONDS,
    )


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the last _MAX_HISTORY_MESSAGES entries for LLM context."""
    if len(history) <= _MAX_HISTORY_MESSAGES:
        return list(history)
    return list(history[-_MAX_HISTORY_MESSAGES:])


def _build_npc_memory_summary(history: list[dict[str, str]]) -> str | None:
    """Build a brief memory summary from the trimmed-off portion of history (#R3).

    When history exceeds _MEMORY_SUMMARY_THRESHOLD messages, the early messages
    get trimmed by _trim_history. This function produces a condensed summary of
    what happened in those lost messages so the NPC retains continuity.
    """
    if len(history) <= _MEMORY_SUMMARY_THRESHOLD:
        return None

    # Summarize the trimmed portion (everything before the sliding window)
    trimmed_count = len(history) - _MAX_HISTORY_MESSAGES
    if trimmed_count <= 0:
        return None

    trimmed = history[:trimmed_count]

    # Extract key points from trimmed turns
    topics = []
    for msg in trimmed:
        if msg["role"] == "user":
            # Take first 80 chars of each player turn as a topic indicator
            text = msg["content"][:80].strip()
            if text:
                topics.append(text)

    if not topics:
        return None

    # Cap the number of remembered topics to avoid bloating the prompt
    topics = topics[-8:]
    summary_lines = [f"- {t}..." if len(t) >= 78 else f"- {t}" for t in topics]

    return f"Earlier in this session ({trimmed_count // 2} turns ago), the player discussed:\n" + "\n".join(
        summary_lines
    )


async def _send_error(ws: WebSocket, code: str, message: str) -> None:
    await ws.send_json({"type": "error", "code": code, "message": message})


async def _flush_connection_analytics(session_id: str, analytics: dict) -> None:
    """Persist per-connection analytics to the session's active scenes (#R4).

    Computes turn_latencies_ms list and aggregated counters, then merges them
    into each scene's analytics JSON field so session-end analytics can aggregate.
    """
    if not session_id or analytics["turn_count"] == 0:
        return

    try:
        from relay.models import GameSession, Scene

        async with _db.AsyncSessionLocal() as db:
            # Find active scenes in this session
            result = await db.execute(
                select(Scene)
                .join(GameSession)
                .where(GameSession.id == session_id)
                .where(Scene.status == "active")
            )
            scenes = list(result.scalars().all())

            if not scenes:
                return

            # Distribute analytics to the most recent active scene
            scene = scenes[-1]
            scene_analytics = dict(scene.analytics or {})

            # Merge connection analytics into scene analytics
            scene_analytics["llm_call_count"] = scene_analytics.get("llm_call_count", 0) + analytics["llm_call_count"]
            scene_analytics["check_pass_count"] = (
                scene_analytics.get("check_pass_count", 0) + analytics["check_pass_count"]
            )
            scene_analytics["check_total_count"] = (
                scene_analytics.get("check_total_count", 0) + analytics["check_total_count"]
            )

            # Compute average turn latency from this connection
            if analytics["turn_count"] > 0:
                avg_latency = analytics["total_turn_latency_ms"] // analytics["turn_count"]
                latencies = scene_analytics.get("turn_latencies_ms", [])
                latencies.append(avg_latency)
                scene_analytics["turn_latencies_ms"] = latencies[-50:]  # Cap

            scene.analytics = scene_analytics
            await db.commit()

        logger.info(
            "Connection analytics flushed",
            extra={
                "session_id": session_id,
                "llm_calls": analytics["llm_call_count"],
                "turns": analytics["turn_count"],
            },
        )
    except Exception:
        # Analytics flush is best-effort — never crash on disconnect
        logger.debug("Failed to flush connection analytics", exc_info=True)


async def _authenticate(ws: WebSocket) -> SessionTokenPayload | None:
    """Authenticate the WebSocket and require a session token.

    Dialogue requires session context (world_id, session_id, mode) so only
    session tokens are accepted — bare account tokens are rejected.
    """
    try:
        raw = await ws.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            await _send_error(ws, "protocol_error", "First message must be type 'auth'")
            return None
        token_str = msg.get("token", "")
        payload = decode_token(token_str)
        if not isinstance(payload, SessionTokenPayload):
            await _send_error(
                ws,
                "unauthorized",
                "Dialogue requires a session token. Start a session first.",
            )
            return None
        return payload
    except jwt.PyJWTError:
        await _send_error(ws, "unauthorized", "Invalid or expired token")
        return None
    except Exception:
        await _send_error(ws, "protocol_error", "Malformed auth message")
        return None


async def _send_recovery_data(ws: WebSocket, player_id: str, *, session_id: str = "") -> None:
    """On reconnect, send any incomplete pending turns so the client can recover.

    (#1) First marks stale turns as failed so they aren't sent as recoverable.
    (#5) Scoped to current session_id to prevent cross-session leakage.
    (#6) Includes created_at so the client can decide whether to resume or abandon.
    (#11) Sends a single batch frame to avoid blocking the handshake.
    """
    # (#1) Clean up stale turns before reporting recovery data
    await mark_stale_turns(player_id, session_id=session_id or None)

    # (#5) Session-scoped recovery
    pending = await get_pending_turns(player_id, session_id=session_id or None)
    if not pending:
        return

    # (#11) Send as a batch frame to minimize blocking
    recovery_batch = []
    for pt in pending:
        recovery_batch.append(
            {
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
                "created_at": pt["created_at"],  # (#6)
                "retry_count": pt["retry_count"],  # (#4)
            }
        )

    await ws.send_json({"type": "turn_recovery", "turns": recovery_batch})

    logger.info(
        "Recovery data sent",
        extra={"player_id": player_id, "pending_count": len(pending)},
    )


@router.websocket("/dialogue")
async def dialogue_ws(ws: WebSocket) -> None:
    await ws.accept()

    token = await _authenticate(ws)
    if token is None:
        await ws.close(code=1008)
        return

    player_id = token.player_id
    world_id = token.world_id  # (#R5) Thread world_id for NPC loading
    session_id = token.session_id  # Session scope for recovery (#5)
    session_mode = token.mode  # "solo" or "multiplayer"
    logger.info("WS authenticated", extra={"player_id": player_id, "mode": session_mode})

    # Send recovery data for any interrupted turns (#5: session-scoped)
    await _send_recovery_data(ws, player_id, session_id=session_id)

    scene_histories: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    last_message_time: float = 0.0
    in_flight = False

    # Single Anthropic client for the lifetime of this connection (#9).
    client = _get_client()

    # Pending check proposals awaiting player confirmation in solo mode.
    # Maps turn_id -> state needed to resume after check_confirm.
    pending_check_proposals: dict[str, dict] = {}

    # (#7) Track already-triggered passive check element IDs per scene
    # to avoid re-firing the same discovery hint every turn.
    triggered_passive_elements: dict[str, list[str]] = {}

    # Per-connection analytics accumulator (#R4)
    connection_analytics: dict = {
        "llm_call_count": 0,
        "turn_count": 0,
        "total_turn_latency_ms": 0,
        "check_pass_count": 0,
        "check_total_count": 0,
    }

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "heartbeat":
                await ws.send_json({"type": "heartbeat_ack"})
                continue

            # check_confirm resumes an existing turn — exempt from rate limit.
            if msg_type == "check_confirm":
                await _handle_check_confirm(
                    ws,
                    msg,
                    client,
                    pending_check_proposals,
                    scene_histories,
                    player_id=player_id,
                    analytics=connection_analytics,
                )
                continue

            now = time.time()
            if now - last_message_time < _RATE_LIMIT_SECONDS:
                await _send_error(ws, "rate_limited", "Please wait before sending another message")
                continue
            last_message_time = now

            # (#2) turn_resume re-enters pipeline from last saved stage.
            # Rate-limited because it triggers new LLM calls.
            if msg_type == "turn_resume":
                await _handle_turn_resume(
                    ws,
                    msg,
                    client,
                    scene_histories,
                    player_id,
                    session_mode=session_mode,
                    pending_check_proposals=pending_check_proposals,
                    world_id=world_id,
                    session_id=session_id,
                    analytics=connection_analytics,
                    triggered_passive_elements=triggered_passive_elements,
                )
                continue

            # (#R8) in_flight guard also covers pending check_confirm state
            if in_flight or pending_check_proposals:
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
                    await _handle_quickchat(ws, msg, history, player_id, client, world_id=world_id)
                elif msg_type == "rp_turn":
                    await _handle_rp_turn(
                        ws,
                        msg,
                        history,
                        player_id,
                        client,
                        session_mode=session_mode,
                        pending_check_proposals=pending_check_proposals,
                        world_id=world_id,
                        session_id=session_id,
                        analytics=connection_analytics,
                        triggered_passive_elements=triggered_passive_elements,
                    )
                else:
                    await _send_error(ws, "unknown_type", f"Unrecognised message type: {msg_type}")
            finally:
                # (#R8) Only release in_flight if no pending check proposals
                if not pending_check_proposals:
                    in_flight = False

    except WebSocketDisconnect:
        logger.info("WS disconnected", extra={"player_id": player_id})
    except Exception:
        logger.exception("WS error", extra={"player_id": player_id})
        try:
            await _send_error(ws, "internal_error", "An unexpected error occurred")
            await ws.close(code=1011)
        except Exception:
            pass

    # Flush per-connection analytics to session on disconnect (#R4).
    if connection_analytics["turn_count"] > 0:
        await _flush_connection_analytics(session_id, connection_analytics)


async def _get_or_load_history(
    scene_histories: OrderedDict[str, list[dict[str, str]]],
    scene_id: str,
) -> list[dict[str, str]] | None:
    """Return the history list for a scene, loading from DB on first access.

    Returns None if *scene_id* is non-empty but does not match any scene
    in the database — callers should reject the turn.

    Caps total scenes per connection to _MAX_SCENES_PER_CONNECTION (#8).
    Uses LRU eviction — most recently accessed scenes survive longest.
    """
    if scene_id in scene_histories:
        # Move to end (most recently used) for LRU ordering.
        scene_histories.move_to_end(scene_id)
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

    # Evict least-recently-used scene if at capacity (#8).
    if len(scene_histories) >= _MAX_SCENES_PER_CONNECTION:
        evicted_key, _ = scene_histories.popitem(last=False)  # Pop oldest (LRU)
        logger.debug("Evicted LRU scene history", extra={"evicted_scene": evicted_key})

    scene_histories[scene_id] = history
    return history


# ---------------------------------------------------------------------------
# Quick-chat handler (single LLM call, no scene state)
# ---------------------------------------------------------------------------


async def _handle_quickchat(
    ws: WebSocket,
    msg: dict,
    history: list[dict[str, str]],
    player_id: str,
    client: anthropic.AsyncAnthropic,
    *,
    world_id: str = "",
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
    if len(player_input) > _MAX_INPUT_LENGTH:
        await _send_error(
            ws,
            "input_too_long",
            f"Text exceeds maximum length of {_MAX_INPUT_LENGTH} characters",
        )
        return

    try:
        npc = await load_npc(npc_id, world_id=world_id or None)  # (#R5)
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
    # Append user message before LLM call for resilience (#7).
    history.append({"role": "user", "content": player_input})

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
            messages=_trim_history(history),
        ) as stream:
            async for text in stream.text_stream:
                full_response += text
                await ws.send_json({"type": "stream_chunk", "turn_id": ws_turn_id, "text": text})

        history.append({"role": "assistant", "content": full_response})
        await ws.send_json(
            {
                "type": "stream_end",
                "turn_id": ws_turn_id,
                "npc_id": npc_id,
                "full_text": full_response,
            }
        )

        if turn_id:
            await complete_turn(turn_id, full_response)

        logger.info("Quick-chat turn complete", extra={"turn_id": ws_turn_id, "npc_id": npc_id})

    except anthropic.APIError as e:
        logger.error("Anthropic API error", extra={"error": str(e)})
        if turn_id:
            await fail_turn(turn_id, str(e))
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


# ---------------------------------------------------------------------------
# RP mode handler (two-call turn flow, with solo check_confirm)
# ---------------------------------------------------------------------------


async def _handle_rp_turn(
    ws: WebSocket,
    msg: dict,
    history: list[dict[str, str]],
    player_id: str,
    client: anthropic.AsyncAnthropic,
    *,
    session_mode: str,
    pending_check_proposals: dict[str, dict],
    world_id: str = "",
    session_id: str = "",
    analytics: dict | None = None,
    triggered_passive_elements: dict[str, list[str]] | None = None,
) -> None:
    """Full RP turn: analysis call -> check resolution -> final prose call.

    In solo mode, checks are proposed to the player for confirmation before
    resolution.  In multiplayer mode, checks resolve automatically.
    """
    turn_start_time = time.time()

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
    if len(player_prose) > _MAX_INPUT_LENGTH:
        await _send_error(
            ws,
            "input_too_long",
            f"Text exceeds maximum length of {_MAX_INPUT_LENGTH} characters",
        )
        return

    try:
        npc = await load_npc(npc_id, world_id=world_id or None)  # (#R5)
    except NpcLoadError as exc:
        logger.error("NPC file corrupt", extra={"npc_id": npc_id, "error": str(exc)})
        await _send_error(ws, "npc_load_error", f"NPC '{npc_id}' exists but failed to load: {exc}")
        return
    if npc is None:
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    # Load scene_state from DB — relay is source of truth (Keystone, #11).
    scene_state: dict = {}
    if scene_id:
        loaded_state = await load_scene_state(scene_id)
        if loaded_state is not None:
            scene_state = loaded_state
    environmental_effects = scene_state.get("environmental_effects", [])

    # Load game-state context from DB for prompt injection (design_proposals §9).
    # Resolves character_id from session, then loads faction standing, relationship
    # with this NPC, active companions, and scene atmosphere.
    game_context: GameStateContext | None = None
    character_id: str | None = None
    char_mechanics: CharacterMechanics | None = None
    if session_id:
        character_id = await resolve_character_id(session_id)
        if character_id:
            game_context = await load_game_context(
                character_id,
                npc_id,
                npc_faction_id=getattr(npc, "faction_id", None),
                scene_state=scene_state,
            )
            # Keystone Principle: load authoritative mechanical stats from DB.
            # NEVER trust client-supplied ability_scores/level/proficiencies
            # for check resolution.
            char_mechanics = await load_character_mechanics(character_id)

    # Use DB-authoritative stats for check resolution; fall back to client data
    # only if character_id resolution failed (e.g. disconnected session).
    if char_mechanics is not None:
        ability_scores = char_mechanics.ability_scores
        skill_profs = char_mechanics.skill_proficiencies
        level = char_mechanics.level
        conditions = char_mechanics.conditions
        exhaustion_level = char_mechanics.exhaustion_level
    else:
        # Fallback: use client-supplied data (degraded mode, logged as warning)
        logger.warning(
            "Using client-supplied character stats (no DB character found)",
            extra={"session_id": session_id, "npc_id": npc_id},
        )
        ability_scores = character_sheet.get("ability_scores", {})
        skill_profs = character_sheet.get("skill_proficiencies", [])
        level = character_sheet.get("level", 1)
        conditions = character_sheet.get("conditions", [])
        exhaustion_level = character_sheet.get("exhaustion_level", 0)

    # (#10) Synthesize exhaustion condition from integer field so the resolver sees it
    if exhaustion_level and exhaustion_level > 0:
        # Only add if not already present in conditions array
        has_exhaustion = any(c.get("condition_id", "").startswith("exhaustion") for c in conditions)
        if not has_exhaustion:
            conditions = [*conditions, {"condition_id": f"exhaustion_{exhaustion_level}", "source": "character_state"}]

    # === Persist pending turn before any processing (Invariant #12) ===
    # (#2) If this is a resumed turn, use the pre-created turn_id
    turn_id = msg.get("_resume_turn_id", "")
    if not turn_id and scene_id:
        turn_id = await create_pending_turn(
            scene_id=scene_id,
            player_id=player_id,
            npc_id=npc_id,
            turn_type="rp",
            player_input=player_prose,
            character_snapshot=character_sheet,
        )

    ws_turn_id = turn_id or f"rp_{int(time.time() * 1000)}"

    # (#R1) System prompt as cache-control blocks (Tier 1 + Tier 2)
    system_prompt = build_rp_system_prompt(npc, game_context=game_context)

    # (#R3) Build NPC memory summary from trimmed history
    npc_memory = _build_npc_memory_summary(history)

    # Append user message before LLM call for resilience (#7).
    history.append({"role": "user", "content": player_prose})

    await ws.send_json({"type": "stream_start", "turn_id": ws_turn_id, "npc_id": npc_id})

    try:
        # === CALL 1: Structured analysis ===
        if turn_id:
            await update_stage(turn_id, "analysis")

        analysis_messages = build_analysis_messages(
            player_prose, _trim_history(history[:-1]), game_context=game_context
        )

        analysis_response = await client.messages.create(
            model=_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=analysis_messages,
            tools=[_ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "scene_analysis"},
        )

        # (#R4) Track LLM call
        if analytics is not None:
            analytics["llm_call_count"] += 1

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
                "checks": [],
                "scene_changes": {},
                "animation_directives": [],
                "draft_response": fallback_text,
                "narrative_signals": [],
                "world_mutations": [],
            }
        logger.debug("Analysis response received", extra={"turn_id": ws_turn_id})

        if turn_id:
            await update_stage(turn_id, "checks_resolved", analysis_result=analysis)

        # === Validate checks (#11: cap at MAX_CHECKS_PER_TURN) ===
        raw_checks = analysis.get("checks", [])
        validated_checks = validate_checks_batch(raw_checks)

        # === Solo mode: propose checks for player confirmation (#1) ===
        if session_mode == "solo" and validated_checks:
            for vc in validated_checks:
                await ws.send_json(
                    {
                        "type": "check_proposal",
                        "turn_id": ws_turn_id,
                        "skill": vc["skill"],
                        "dc": vc["dc"],
                        "reason": vc["reason"],
                        "advantage": vc["advantage"],
                        "disadvantage": vc["disadvantage"],
                    }
                )

            # Stash state and return — the turn resumes on check_confirm.
            pending_check_proposals[ws_turn_id] = {
                "turn_id": turn_id,
                "ws_turn_id": ws_turn_id,
                "npc_id": npc_id,
                "npc": npc,
                "system_prompt": system_prompt,
                "player_prose": player_prose,
                "history": history,
                "scene_id": scene_id,
                "analysis": analysis,
                "validated_checks": validated_checks,
                "ability_scores": ability_scores,
                "skill_profs": skill_profs,
                "level": level,
                "conditions": conditions,
                "environmental_effects": environmental_effects,
                "scene_state": scene_state,
                "npc_memory": npc_memory,
                "turn_start_time": turn_start_time,
                "player_id": player_id,  # (#R9) For ownership validation
                "triggered_passive_elements": triggered_passive_elements,
                "character_id": character_id,
                "session_id": session_id,
                "world_id": world_id,
            }
            logger.info(
                "Check proposals sent, awaiting player confirmation",
                extra={"turn_id": ws_turn_id, "check_count": len(validated_checks)},
            )
            return

        # === Multiplayer / no checks: resolve immediately ===
        await _resolve_and_finish_rp(
            ws=ws,
            client=client,
            turn_id=turn_id,
            ws_turn_id=ws_turn_id,
            npc_id=npc_id,
            npc=npc,
            system_prompt=system_prompt,
            player_prose=player_prose,
            history=history,
            scene_id=scene_id,
            analysis=analysis,
            validated_checks=validated_checks,
            ability_scores=ability_scores,
            skill_profs=skill_profs,
            level=level,
            conditions=conditions,
            environmental_effects=environmental_effects,
            scene_state=scene_state,
            npc_memory=npc_memory,
            turn_start_time=turn_start_time,
            analytics=analytics,
            triggered_passive_elements=triggered_passive_elements,
            character_id=character_id,
            session_id=session_id,
            world_id=world_id,
        )

    except anthropic.APIError as e:
        logger.error("Anthropic API error during RP turn", extra={"error": str(e), "turn_id": ws_turn_id})
        if turn_id:
            await fail_turn(turn_id, str(e))
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


async def _handle_check_confirm(
    ws: WebSocket,
    msg: dict,
    client: anthropic.AsyncAnthropic,
    pending_check_proposals: dict[str, dict],
    scene_histories: dict[str, list[dict[str, str]]],
    *,
    player_id: str,
    analytics: dict | None = None,
) -> None:
    """Handle a check_confirm message from the client (solo mode).

    The client sends this after reviewing check_proposal frames.
    Payload: { "type": "check_confirm", "turn_id": "<ws_turn_id>" }
    """
    ws_turn_id = msg.get("turn_id", "")
    if ws_turn_id not in pending_check_proposals:
        await _send_error(ws, "not_found", f"No pending check proposals for turn '{ws_turn_id}'")
        return

    state = pending_check_proposals[ws_turn_id]

    # (#R9) Verify the confirming player owns this turn
    if state.get("player_id") != player_id:
        await _send_error(ws, "unauthorized", "Cannot confirm another player's checks")
        return

    # Pop state — turn is being resolved now
    pending_check_proposals.pop(ws_turn_id)

    try:
        await _resolve_and_finish_rp(
            ws=ws,
            client=client,
            turn_id=state["turn_id"],
            ws_turn_id=state["ws_turn_id"],
            npc_id=state["npc_id"],
            npc=state["npc"],
            system_prompt=state["system_prompt"],
            player_prose=state["player_prose"],
            history=state["history"],
            scene_id=state["scene_id"],
            analysis=state["analysis"],
            validated_checks=state["validated_checks"],
            ability_scores=state["ability_scores"],
            skill_profs=state["skill_profs"],
            character_id=state.get("character_id"),
            session_id=state.get("session_id", ""),
            world_id=state.get("world_id", ""),
            level=state["level"],
            conditions=state["conditions"],
            environmental_effects=state["environmental_effects"],
            scene_state=state["scene_state"],
            npc_memory=state.get("npc_memory"),
            turn_start_time=state.get("turn_start_time", time.time()),
            analytics=analytics,
            triggered_passive_elements=state.get("triggered_passive_elements"),
        )
    except anthropic.APIError as e:
        logger.error(
            "Anthropic API error during check_confirm",
            extra={"error": str(e), "turn_id": ws_turn_id},
        )
        if state["turn_id"]:
            await fail_turn(state["turn_id"], str(e))
        await _send_error(ws, "llm_error", "Failed to get NPC response. Try again.")


async def _resolve_and_finish_rp(
    *,
    ws: WebSocket,
    client: anthropic.AsyncAnthropic,
    turn_id: str,
    ws_turn_id: str,
    npc_id: str,
    npc: object,
    system_prompt: list[dict],
    player_prose: str,
    history: list[dict[str, str]],
    scene_id: str,
    analysis: dict,
    validated_checks: list[dict],
    ability_scores: dict[str, int],
    skill_profs: list[str],
    level: int,
    conditions: list[dict],
    environmental_effects: list[str],
    scene_state: dict,
    npc_memory: str | None = None,
    turn_start_time: float | None = None,
    analytics: dict | None = None,
    triggered_passive_elements: dict[str, list[str]] | None = None,
    chain_depth: int = 0,
    character_id: str | None = None,
    session_id: str = "",
    world_id: str = "",
) -> None:
    """Resolve checks, send results/animations/scene, then stream final prose."""
    if chain_depth >= _MAX_AI_CHAIN_DEPTH:
        logger.warning("AI chain depth exceeded", extra={"depth": chain_depth, "turn_id": ws_turn_id})
        await ws.send_json(
            {"type": "chain_limit", "turn_id": ws_turn_id, "message": "Scene paused — AI chain depth exceeded"}
        )
        if turn_id:
            await fail_turn(turn_id, f"chain_depth_exceeded ({chain_depth})")
        return

    check_results = []

    for vc in validated_checks:
        result = resolve_check(
            vc,
            ability_scores,
            skill_profs,
            level,
            conditions=conditions,
            environmental_effects=environmental_effects,
        )
        check_results.append(result)
        await ws.send_json(
            {
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
                "natural_20": result.get("natural_20", False),
                "natural_1": result.get("natural_1", False),
            }
        )

    # (#R4) Track check results for analytics
    if analytics is not None:
        analytics["check_total_count"] += len(check_results)
        analytics["check_pass_count"] += sum(1 for cr in check_results if cr["passed"])

    # === Evaluate passive checks (Invariant #22) ===
    # (#7) Pass already-triggered element IDs to avoid re-firing
    already_triggered = []
    if triggered_passive_elements is not None and scene_id:
        already_triggered = triggered_passive_elements.get(scene_id, [])

    passive_hints = evaluate_passive_checks(
        ability_scores,
        skill_profs,
        level,
        scene_state,
        conditions=conditions,
        already_triggered=already_triggered,
    )

    # (#7) Record newly triggered elements so they don't fire again
    if triggered_passive_elements is not None and scene_id and passive_hints:
        if scene_id not in triggered_passive_elements:
            triggered_passive_elements[scene_id] = []
        triggered_passive_elements[scene_id].extend(h["element_id"] for h in passive_hints)

    for hint in passive_hints:
        await ws.send_json(
            {
                "type": "passive_check",
                "turn_id": ws_turn_id,
                "skill": hint["skill"],
                "dc": hint["dc"],
                "passive_value": hint["passive_value"],
                "element_id": hint["element_id"],
            }
        )

    # === Send animation directives ===
    valid_animations = _validate_animation_directives(
        analysis.get("animation_directives", []),
        npc,
    )
    for anim in valid_animations:
        await ws.send_json(
            {
                "type": "animation_directive",
                "turn_id": ws_turn_id,
                "target": anim["target"],
                "directive": anim["directive"],
            }
        )

    # === Send scene update ===
    scene_changes = analysis.get("scene_changes", {})
    if scene_changes:
        await ws.send_json(
            {
                "type": "scene_update",
                "turn_id": ws_turn_id,
                "changes": scene_changes,
            }
        )

    # === Persist narrative signals (§6 Three-Layer Narrative) ===
    raw_signals = analysis.get("narrative_signals", [])
    if raw_signals and character_id:
        validated_signals = validate_narrative_signals(raw_signals)
        if validated_signals:
            async with _db.AsyncSessionLocal() as signal_db:
                for sig in validated_signals:
                    await upsert_thread(
                        signal_db,
                        character_id=character_id,
                        world_id=world_id,
                        signal=sig,
                        session_id=session_id if session_id else None,
                    )
                await signal_db.commit()
            logger.info(
                "Narrative signals persisted",
                extra={
                    "turn_id": ws_turn_id,
                    "signals_count": len(validated_signals),
                },
            )

    # === Apply world mutations (consequence system §7, §8) ===
    raw_mutations = analysis.get("world_mutations", [])
    mutation_report: dict = {}
    if raw_mutations and character_id:
        validated_mutations = validate_world_mutations(raw_mutations)
        # §8: Filter mutations based on NPC's consequence profile
        npc_profile = resolve_profile(getattr(npc, "consequence_profile", None))
        validated_mutations = filter_mutations_by_profile(validated_mutations, npc_profile)
        if validated_mutations:
            async with _db.AsyncSessionLocal() as mutation_db:
                # Load character's faction_standing and relationships from DB
                char_result = await mutation_db.execute(select(Character).where(Character.id == character_id))
                char = char_result.scalar_one_or_none()

                faction_standing: dict[str, int] | None = None
                relationships: dict[str, int] | None = None
                faction_registry: dict[str, dict] | None = None

                if char is not None:
                    # Copy dicts — apply_standing_change and
                    # apply_relationship_change mutate in place
                    faction_standing = dict(char.faction_standing or {})
                    relationships = dict(char.relationships or {})

                    # Load faction definitions for propagation
                    if world_id:
                        faction_registry = await load_faction_registry(world_id)

                mutation_report = await apply_world_mutations(
                    mutation_db,
                    character_id=character_id,
                    mutations=validated_mutations,
                    faction_standing=faction_standing,
                    relationships=relationships,
                    faction_registry=faction_registry,
                    session_id=session_id if session_id else None,
                )

                # Persist updated dicts back to character (Keystone: relay is
                # source of truth for all persistent state)
                if char is not None:
                    if mutation_report.get("faction_changes") and faction_standing is not None:
                        char.faction_standing = faction_standing
                        flag_modified(char, "faction_standing")
                    if mutation_report.get("relationship_changes") and relationships is not None:
                        char.relationships = relationships
                        flag_modified(char, "relationships")

                await mutation_db.commit()

            if mutation_report:
                logger.info(
                    "World mutations applied",
                    extra={
                        "turn_id": ws_turn_id,
                        "flags_set": len(mutation_report.get("flags_set", [])),
                        "faction_changes": len(mutation_report.get("faction_changes", [])),
                        "relationship_changes": len(mutation_report.get("relationship_changes", [])),
                    },
                )

    if turn_id:
        await update_stage(
            turn_id,
            "streaming",
            check_results=check_results,
            animation_directives=valid_animations,
            scene_changes=scene_changes,
        )

    # === CALL 2: Final prose with check results ===
    draft = analysis.get("draft_response", "")
    # (#R10) Include passive hints in final prose call
    final_messages = build_final_prose_messages(
        player_prose,
        draft,
        check_results,
        _trim_history(history[:-1]),
        passive_hints=passive_hints if passive_hints else None,
        npc_memory_summary=npc_memory,
    )

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

    # (#R4) Track second LLM call
    if analytics is not None:
        analytics["llm_call_count"] += 1

    # === Commit assistant response to history ===
    history.append({"role": "assistant", "content": full_response})

    await ws.send_json(
        {
            "type": "stream_end",
            "turn_id": ws_turn_id,
            "npc_id": npc_id,
            "full_text": full_response,
        }
    )

    # (#R6) Atomic commit: pending turn + scene state + turn history in one transaction
    # (#8) Also persist check_results for consistency
    if turn_id:
        await complete_turn(
            turn_id,
            full_response,
            scene_changes=scene_changes,
            check_results=check_results if check_results else None,
        )

    # (#R4) Track turn timing
    if analytics is not None and turn_start_time is not None:
        latency_ms = int((time.time() - turn_start_time) * 1000)
        analytics["turn_count"] += 1
        analytics["total_turn_latency_ms"] += latency_ms

    logger.info(
        "RP turn complete",
        extra={
            "turn_id": ws_turn_id,
            "npc_id": npc_id,
            "checks_resolved": len(check_results),
            "passive_checks_triggered": len(passive_hints),
            "latency_ms": int((time.time() - turn_start_time) * 1000) if turn_start_time else None,
        },
    )


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
                    "emotional_temperature_delta": {"type": "number", "minimum": -0.3, "maximum": 0.3},
                    "notes": {"type": "string"},
                    "environment_add": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "darkness",
                                "difficult_terrain",
                                "high_ground",
                                "extreme_weather",
                                "hazard",
                            ],
                        },
                        "description": "Environmental effects to add to the scene.",
                    },
                    "environment_remove": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "darkness",
                                "difficult_terrain",
                                "high_ground",
                                "extreme_weather",
                                "hazard",
                            ],
                        },
                        "description": "Environmental effects to remove from the scene.",
                    },
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
            "narrative_signals": {
                "type": "array",
                "description": (
                    "Lightweight signals about player commitments, interests, "
                    "revelations, or rising tension noticed during this turn.  "
                    "Relay persists these as narrative threads for the director."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["commitment", "interest", "revelation", "tension"],
                        },
                        "summary": {
                            "type": "string",
                            "description": "One-sentence description of the signal.",
                        },
                        "thread_key": {
                            "type": "string",
                            "description": (
                                "Short snake_case key identifying the narrative "
                                "thread, e.g. 'missing_brother', 'guild_corruption'."
                            ),
                        },
                        "related_npcs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "NPC IDs involved in this thread.",
                        },
                        "related_regions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Region IDs relevant to this thread.",
                        },
                    },
                    "required": ["type", "summary", "thread_key"],
                },
            },
            "world_mutations": {
                "type": "array",
                "description": (
                    "Proposed faction, relationship, or world-flag changes "
                    "caused by this turn's narrative events. Relay validates "
                    "magnitudes and applies through existing systems."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "faction_standing_change",
                                "relationship_change",
                                "world_flag_set",
                            ],
                        },
                        "faction_id": {
                            "type": "string",
                            "description": "Required for faction_standing_change.",
                        },
                        "npc_id": {
                            "type": "string",
                            "description": "Required for relationship_change.",
                        },
                        "flag": {
                            "type": "string",
                            "description": "Required for world_flag_set.",
                        },
                        "delta": {
                            "type": "integer",
                            "minimum": -50,
                            "maximum": 50,
                            "description": "Signed change for faction or relationship.",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["type", "reason"],
                },
            },
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


# ---------------------------------------------------------------------------
# Turn resume handler (#2)
# ---------------------------------------------------------------------------


async def _handle_turn_resume(
    ws: WebSocket,
    msg: dict,
    client: anthropic.AsyncAnthropic,
    scene_histories: OrderedDict[str, list[dict[str, str]]],
    player_id: str,
    *,
    session_mode: str,
    pending_check_proposals: dict[str, dict],
    world_id: str = "",
    session_id: str = "",
    analytics: dict | None = None,
    triggered_passive_elements: dict[str, list[str]] | None = None,
) -> None:
    """Handle a turn_resume message — re-enter pipeline from last saved stage (#2).

    Client sends: { "type": "turn_resume", "turn_id": "<pending_turn_id>" }

    Resume logic based on stage:
      - "streaming" with final_response: deliver the saved response (no LLM call)
      - "checks_resolved" or "analysis": re-run from that point
      - "received": re-run full pipeline

    If retry_count >= MAX_RETRIES, reject with an error.
    """
    turn_id = msg.get("turn_id", "")
    if not turn_id:
        await _send_error(ws, "missing_field", "turn_resume requires 'turn_id'")
        return

    pt = await get_pending_turn(turn_id)
    if pt is None:
        await _send_error(ws, "not_found", f"Pending turn '{turn_id}' not found")
        return

    # Ownership check
    if pt["player_id"] != player_id:
        await _send_error(ws, "unauthorized", "Cannot resume another player's turn")
        return

    # Terminal states can't be resumed
    if pt["stage"] in ("complete", "failed"):
        await _send_error(
            ws,
            "invalid_state",
            f"Turn is already '{pt['stage']}' — cannot resume",
        )
        return

    # (#4) Check retry cap
    if pt["retry_count"] >= MAX_RETRIES:
        await fail_turn(turn_id, f"max_retries_exceeded ({MAX_RETRIES})")
        await _send_error(
            ws,
            "max_retries",
            f"Turn has exceeded maximum retry attempts ({MAX_RETRIES})",
        )
        return

    scene_id = pt["scene_id"]
    npc_id = pt["npc_id"]

    # If final_response is already saved, just deliver it without re-running LLM
    if pt["stage"] == "streaming" and pt["final_response"]:
        logger.info(
            "Resuming turn with saved final_response",
            extra={"turn_id": turn_id, "stage": pt["stage"]},
        )
        await ws.send_json({"type": "stream_start", "turn_id": turn_id, "npc_id": npc_id})
        await ws.send_json({"type": "stream_chunk", "turn_id": turn_id, "text": pt["final_response"]})
        await ws.send_json(
            {
                "type": "stream_end",
                "turn_id": turn_id,
                "npc_id": npc_id,
                "full_text": pt["final_response"],
            }
        )

        # Complete the turn
        await complete_turn(
            turn_id,
            pt["final_response"],
            scene_changes=pt["scene_changes"],
            check_results=pt["check_results"],
        )

        # Update in-memory history
        history = await _get_or_load_history(scene_histories, scene_id)
        if history is not None:
            history.append({"role": "user", "content": pt["player_input"]})
            history.append({"role": "assistant", "content": pt["final_response"]})

        return

    # For earlier stages, we need to re-run the LLM pipeline.
    # Load NPC and scene state fresh.
    try:
        npc = await load_npc(npc_id, world_id=world_id or None)
    except NpcLoadError as exc:
        await fail_turn(turn_id, f"npc_load_error: {exc}")
        await _send_error(ws, "npc_load_error", f"NPC '{npc_id}' failed to load: {exc}")
        return
    if npc is None:
        await fail_turn(turn_id, f"npc_not_found: {npc_id}")
        await _send_error(ws, "not_found", f"NPC '{npc_id}' not found")
        return

    # Reconstruct character data from snapshot (#7)
    character_sheet = pt["character_snapshot"] or {}

    # Load history and scene state
    history = await _get_or_load_history(scene_histories, scene_id)
    if history is None:
        await fail_turn(turn_id, "scene_not_found")
        await _send_error(ws, "not_found", f"Scene '{scene_id}' not found")
        return

    # Re-dispatch as an rp_turn using the stored data
    synthetic_msg = {
        "type": "rp_turn",
        "npc_id": npc_id,
        "scene_id": scene_id,
        "text": pt["player_input"],
        "character": character_sheet,
    }

    # Mark the old turn as failed (it's being superseded by a retry)
    await fail_turn(turn_id, "resumed_by_client")

    # Increment retry count for the new attempt
    new_retry_count = pt["retry_count"] + 1

    # Create a new pending turn linked to the parent (#4)
    new_turn_id = await create_pending_turn(
        scene_id=scene_id,
        player_id=player_id,
        npc_id=npc_id,
        turn_type=pt["turn_type"],
        player_input=pt["player_input"],
        character_snapshot=character_sheet,
        parent_turn_id=turn_id,
        retry_count=new_retry_count,
    )

    logger.info(
        "Turn resumed as new attempt",
        extra={
            "original_turn_id": turn_id,
            "new_turn_id": new_turn_id,
            "retry_count": new_retry_count,
            "from_stage": pt["stage"],
        },
    )

    # Run the full RP turn with the new pending turn
    # (We override scene_id in the message to ensure the new turn_id is used)
    synthetic_msg["_resume_turn_id"] = new_turn_id

    await _handle_rp_turn(
        ws,
        synthetic_msg,
        history,
        player_id,
        client,
        session_mode=session_mode,
        pending_check_proposals=pending_check_proposals,
        world_id=world_id,
        session_id=session_id,
        analytics=analytics,
        triggered_passive_elements=triggered_passive_elements,
    )
