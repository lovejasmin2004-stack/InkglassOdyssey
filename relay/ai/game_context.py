"""Game-state context for prompt injection.

Loads character game-state from the database and formats it for inclusion
in NPC prompts.  This bridges the gap identified in design_proposals.md §9:
faction_standing, relationships, companions, and scene context all exist in
the relay but previously were never forwarded to the prompt builder.

Architecture:
  - Tier 1 (static, cache aggressively): NPC personality
  - Tier 2 (session-stable): game-state context (this module)
  - Tier 3 (dynamic, never cached): player input, check results
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

import relay.database as _db
from relay.companions.relationship import resolve_relationship_tier
from relay.consequences.npc_state import get_instance
from relay.consequences.world_flags import get_flags
from relay.factions.reputation import resolve_tier
from relay.models import Character, GameSession
from relay.narrative.threads import get_active_threads

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GameStateContext:
    """Immutable snapshot of character game-state relevant to NPC dialogue.

    Loaded once per turn from the relay database (source of truth).
    Passed to prompt builders for Tier 2 context injection.
    """

    # Faction standing for the NPC's faction (if NPC has one)
    npc_faction_id: str | None = None
    npc_faction_standing: int = 0
    npc_faction_tier: str = "neutral"

    # Relationship with the current NPC
    npc_relationship_score: int = 0
    npc_relationship_tier: str = "neutral"

    # Active companions (names only — no mechanical stats to LLM)
    active_companion_names: list[str] = field(default_factory=list)

    # NPC instance state (consequence system §7)
    npc_status: str = "alive"
    npc_disposition_override: int | None = None
    npc_flags: list[str] = field(default_factory=list)
    npc_last_interaction: str | None = None

    # Active narrative threads (§6 Three-Layer Narrative)
    active_narrative_threads: list[dict] = field(default_factory=list)

    # World flags (consequence system §7 — wanted status, bounties, etc.)
    world_flags: list[str] = field(default_factory=list)

    # Scene-state summary
    scene_emotional_temperature: float = 0.5
    scene_environment: list[str] = field(default_factory=list)
    director_signal: str | None = None


@dataclass(frozen=True, slots=True)
class CharacterMechanics:
    """Authoritative mechanical stats loaded from the database.

    The relay DB is the source of truth for all persistent state (Keystone
    Principle).  These values are used for check resolution — never trust
    client-supplied stats for mechanical outcomes.
    """

    character_id: str
    ability_scores: dict[str, int] = field(default_factory=dict)
    skill_proficiencies: list[str] = field(default_factory=list)
    level: int = 1
    conditions: list[dict] = field(default_factory=list)
    exhaustion_level: int = 0


async def resolve_character_id(session_id: str) -> str | None:
    """Look up the character_id for an active session.

    Returns None if the session doesn't exist or has no character.
    """
    async with _db.AsyncSessionLocal() as db:
        result = await db.execute(select(GameSession.character_id).where(GameSession.id == session_id))
        row = result.scalar_one_or_none()
    return row


async def load_character_mechanics(character_id: str) -> CharacterMechanics | None:
    """Load authoritative mechanical stats from the database.

    These are the values that MUST be used for check resolution, overriding
    anything the client sends.  Enforces the Keystone Principle: relay is the
    source of truth for all persistent state.

    Returns None if the character doesn't exist.
    """
    async with _db.AsyncSessionLocal() as db:
        result = await db.execute(select(Character).where(Character.id == character_id))
        char = result.scalar_one_or_none()

    if char is None:
        logger.warning(
            "Character not found for mechanics load",
            extra={"character_id": character_id},
        )
        return None

    return CharacterMechanics(
        character_id=character_id,
        ability_scores=dict(char.ability_scores or {}),
        skill_proficiencies=list(char.skill_proficiencies or []),
        level=char.level,
        conditions=list(char.conditions or []),
        exhaustion_level=char.exhaustion_level,
    )


async def load_game_context(
    character_id: str,
    npc_id: str,
    *,
    npc_faction_id: str | None = None,
    scene_state: dict | None = None,
) -> GameStateContext:
    """Load game-state context from the database for prompt injection.

    Opens its own short-lived DB session (same pattern as pending_turns.py).
    Returns a frozen GameStateContext snapshot.

    Parameters
    ----------
    character_id
        The player character's ID.
    npc_id
        The NPC being spoken to (for relationship lookup).
    npc_faction_id
        The NPC's faction_id from their personality file.
    scene_state
        The current scene_state dict (already loaded by dialogue handler).
    """
    async with _db.AsyncSessionLocal() as db:
        result = await db.execute(select(Character).where(Character.id == character_id))
        char = result.scalar_one_or_none()

        # Load NPC instance state (consequence system §7)
        npc_instance = await get_instance(db, character_id, npc_id) if char else None

        # Load active narrative threads (§6 Three-Layer Narrative)
        active_threads_raw = await get_active_threads(db, character_id) if char else []

        # Load world flags (§7 consequence system — wanted status, bounties)
        world_flag_rows = await get_flags(db, character_id) if char else []

    if char is None:
        logger.warning(
            "Character not found for game context",
            extra={"character_id": character_id},
        )
        return GameStateContext()

    # --- Faction standing for this NPC's faction ---
    # Faction values can be int or dict ({"score": N, "reason": "..."})
    faction_standing_map = dict(char.faction_standing or {})
    npc_faction_standing = 0
    npc_faction_tier = "neutral"
    if npc_faction_id:
        raw_fs = faction_standing_map.get(npc_faction_id, 0)
        if isinstance(raw_fs, dict):
            npc_faction_standing = raw_fs.get("score", 0)
        elif isinstance(raw_fs, int):
            npc_faction_standing = raw_fs
        npc_faction_tier = resolve_tier(npc_faction_standing)

    # --- Relationship with this NPC ---
    # Relationship values can be int or dict ({"score": N, "reason": "..."})
    relationships = dict(char.relationships or {})
    raw_rel = relationships.get(npc_id, 0)
    if isinstance(raw_rel, dict):
        npc_relationship_score = raw_rel.get("score", 0)
    elif isinstance(raw_rel, int):
        npc_relationship_score = raw_rel
    else:
        npc_relationship_score = 0
    npc_relationship_tier = resolve_relationship_tier(npc_relationship_score)

    # --- Active companions (names only, no stats) ---
    companions = list(char.companions or [])
    active_companion_names = [c.get("npc_id", "unknown") for c in companions if c.get("active", True)]

    # --- Scene state ---
    scene = scene_state or {}
    scene_emotional_temperature = scene.get("emotional_temperature", 0.5)
    scene_environment = scene.get("environmental_effects", [])
    director_signal = scene.get("director_signal")

    # --- NPC instance state (if tracked) ---
    npc_status = "alive"
    npc_disposition_override: int | None = None
    npc_flags: list[str] = []
    npc_last_interaction: str | None = None
    if npc_instance is not None:
        npc_status = npc_instance.status
        npc_disposition_override = npc_instance.disposition_override
        npc_flags = list(npc_instance.flags or [])
        npc_last_interaction = npc_instance.last_interaction_summary

    # --- Active narrative threads (lightweight dicts for frozen dataclass) ---
    active_narrative_threads: list[dict] = []
    for t in active_threads_raw:
        active_narrative_threads.append(
            {
                "thread_key": t.thread_key,
                "type": t.signal_type,
                "summary": t.summary,
                "mentions": t.mention_count,
                "related_npcs": list(t.related_npcs or []),
                "related_regions": list(t.related_regions or []),
            }
        )

    # --- World flags (§7 — wanted status, bounty, quest milestones) ---
    # Present as flag keys only — NPC should react to player's world state.
    # Cap to 10 flags to avoid prompt bloat.
    world_flags = [wf.flag for wf in world_flag_rows[:10]]

    return GameStateContext(
        npc_faction_id=npc_faction_id,
        npc_faction_standing=npc_faction_standing,
        npc_faction_tier=npc_faction_tier,
        npc_relationship_score=npc_relationship_score,
        npc_relationship_tier=npc_relationship_tier,
        npc_status=npc_status,
        npc_disposition_override=npc_disposition_override,
        npc_flags=npc_flags,
        npc_last_interaction=npc_last_interaction,
        active_companion_names=active_companion_names,
        active_narrative_threads=active_narrative_threads,
        world_flags=world_flags,
        scene_emotional_temperature=scene_emotional_temperature,
        scene_environment=scene_environment,
        director_signal=director_signal,
    )


def format_context_block(ctx: GameStateContext, npc_name: str) -> str:
    """Format the game-state context as a prompt text block.

    Returns an empty string if there's nothing meaningful to inject
    (all defaults, no relationship, no companions, no scene effects).
    """
    sections: list[str] = []

    # NPC instance state — critical for consequence-aware behaviour
    if ctx.npc_status != "alive":
        sections.append(f"YOUR CURRENT STATUS: {ctx.npc_status}")
    if ctx.npc_disposition_override is not None:
        if ctx.npc_disposition_override <= -60:
            sections.append("YOUR DISPOSITION TOWARD PLAYER: deeply hostile (override)")
        elif ctx.npc_disposition_override <= -30:
            sections.append("YOUR DISPOSITION TOWARD PLAYER: hostile (override)")
        elif ctx.npc_disposition_override >= 60:
            sections.append("YOUR DISPOSITION TOWARD PLAYER: deeply trusting (override)")
        elif ctx.npc_disposition_override >= 30:
            sections.append("YOUR DISPOSITION TOWARD PLAYER: friendly (override)")
    if ctx.npc_flags:
        sections.append(f"FLAGS: {', '.join(ctx.npc_flags)}")
    if ctx.npc_last_interaction:
        sections.append(f"LAST SIGNIFICANT INTERACTION: {ctx.npc_last_interaction}")

    # Relationship context — always include unless score is 0 with neutral tier
    if ctx.npc_relationship_score != 0 or ctx.npc_relationship_tier != "neutral":
        sections.append(
            f"RELATIONSHIP WITH PLAYER: {ctx.npc_relationship_tier} (score {ctx.npc_relationship_score}/100)"
        )

    # Faction reputation context
    if ctx.npc_faction_id and ctx.npc_faction_tier != "neutral":
        sections.append(
            f"PLAYER'S REPUTATION WITH YOUR FACTION ({ctx.npc_faction_id}): "
            f"{ctx.npc_faction_tier} (standing {ctx.npc_faction_standing}/100)"
        )

    # Companion presence
    if ctx.active_companion_names:
        names = ", ".join(ctx.active_companion_names)
        sections.append(f"COMPANIONS PRESENT: {names}")

    # World flags (§7 — wanted status, bounty, milestones)
    if ctx.world_flags:
        sections.append(f"WORLD STATE: {', '.join(ctx.world_flags)}")

    # Active narrative threads (§6 — helps the LLM weave recurring themes)
    if ctx.active_narrative_threads:
        thread_lines: list[str] = []
        for t in ctx.active_narrative_threads[:5]:  # Cap to avoid prompt bloat
            mentions = t.get("mentions", 1)
            label = f"{t['thread_key']} ({t['type']}, {mentions}x): {t['summary']}"
            thread_lines.append(label)
        sections.append("NARRATIVE THREADS:\n  " + "\n  ".join(thread_lines))

    # Scene atmosphere
    scene_parts: list[str] = []
    if ctx.scene_environment:
        scene_parts.append(f"Environment: {', '.join(ctx.scene_environment)}")
    if ctx.scene_emotional_temperature != 0.5:
        if ctx.scene_emotional_temperature < 0.3:
            scene_parts.append("Mood: tense/hostile")
        elif ctx.scene_emotional_temperature > 0.7:
            scene_parts.append("Mood: warm/friendly")
    if scene_parts:
        sections.append("SCENE: " + ". ".join(scene_parts))

    # Director signal (narrative pacing nudge)
    if ctx.director_signal:
        sections.append(f"NARRATIVE DIRECTION: {ctx.director_signal}")

    if not sections:
        return ""

    header = "CURRENT GAME STATE (use to inform your tone and reactions):"
    return header + "\n" + "\n".join(f"- {s}" for s in sections)
