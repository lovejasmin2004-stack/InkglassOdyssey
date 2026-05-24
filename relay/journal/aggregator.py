"""World journal aggregator — read-only view of player progress.

Assembles the WorldJournal from existing relay data:
- People met: NPC instance state + character relationships + NPC notes
- Places visited: scenes with distinct region mentions
- Items found: character inventory
- Factions: character faction_standing
- Story so far: session summaries in chronological order

This is a view layer, not a data store.  All data comes from existing
tables — no new writes required.

Design doc: docs/design_proposals.md §2 (World Journal)
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import (
    Character,
    CharacterPreferences,
    GameSession,
    NpcInstanceState,
    Scene,
)
from relay.schemas import (
    JournalFactionEntry,
    JournalNpcEntry,
    JournalRegionEntry,
    WorldJournal,
)

logger = logging.getLogger(__name__)

# Maximum entries per section to keep the response manageable
_MAX_NPCS = 50
_MAX_REGIONS = 30
_MAX_ITEMS = 100
_MAX_SESSIONS = 20


async def build_journal(
    db: AsyncSession,
    character_id: str,
    world_id: str,
) -> WorldJournal:
    """Build a WorldJournal for the given character.

    Aggregates data from multiple tables into a single read-only view.

    Parameters
    ----------
    db
        Async database session.
    character_id
        The player character whose journal to build.
    world_id
        The world to scope the journal to.

    Returns
    -------
    WorldJournal
        Aggregated journal with all sections populated.
    """
    # --- Load character ---
    char_result = await db.execute(sa.select(Character).where(Character.id == character_id))
    char = char_result.scalar_one_or_none()
    if char is None:
        return WorldJournal(character_id=character_id, world_id=world_id)

    # --- People met ---
    people = await _build_people_met(db, character_id, world_id, char)

    # --- Places visited ---
    places = await _build_places_visited(db, character_id)

    # --- Items found ---
    items = _build_items_found(char)

    # --- Factions ---
    factions = _build_factions(char)

    # --- Story so far ---
    story = await _build_story_so_far(db, character_id)

    return WorldJournal(
        character_id=character_id,
        world_id=world_id,
        people_met=people,
        places_visited=places,
        items_found=items,
        factions=factions,
        story_so_far=story,
    )


async def _build_people_met(
    db: AsyncSession,
    character_id: str,
    world_id: str,
    char: Character,
) -> list[JournalNpcEntry]:
    """Build the 'People I've Met' section from NPC instance state + relationships."""
    entries: list[JournalNpcEntry] = []

    # Get NPC instance states (created on first meaningful interaction)
    nis_result = await db.execute(
        sa.select(NpcInstanceState)
        .where(
            NpcInstanceState.character_id == character_id,
            NpcInstanceState.world_id == world_id,
        )
        .order_by(NpcInstanceState.updated_at.desc())
        .limit(_MAX_NPCS)
    )
    nis_rows = nis_result.scalars().all()

    # Load player's NPC notes from preferences
    prefs_result = await db.execute(
        sa.select(CharacterPreferences).where(CharacterPreferences.character_id == character_id)
    )
    prefs = prefs_result.scalar_one_or_none()
    npc_notes: dict[str, str] = prefs.npc_notes if prefs else {}

    # Build from NPC instance state (most reliable source of "met" NPCs)
    seen_ids: set[str] = set()
    for nis in nis_rows:
        rel_score = 0
        if char.relationships and isinstance(char.relationships, dict):
            rel_data = char.relationships.get(nis.npc_id)
            if isinstance(rel_data, dict):
                rel_score = rel_data.get("score", 0)
            elif isinstance(rel_data, int):
                rel_score = rel_data

        entries.append(
            JournalNpcEntry(
                npc_id=nis.npc_id,
                relationship_score=rel_score,
                last_scene_summary=nis.last_interaction_summary,
                player_notes=npc_notes.get(nis.npc_id),
            )
        )
        seen_ids.add(nis.npc_id)

    # Also include NPCs from relationships dict that don't have instance state
    if char.relationships and isinstance(char.relationships, dict):
        for npc_id, rel_data in char.relationships.items():
            if npc_id in seen_ids:
                continue
            score = rel_data if isinstance(rel_data, int) else rel_data.get("score", 0)
            entries.append(
                JournalNpcEntry(
                    npc_id=npc_id,
                    relationship_score=score,
                    player_notes=npc_notes.get(npc_id),
                )
            )
            seen_ids.add(npc_id)
            if len(entries) >= _MAX_NPCS:
                break

    return entries


async def _build_places_visited(
    db: AsyncSession,
    character_id: str,
) -> list[JournalRegionEntry]:
    """Build 'Places I've Been' from scene data.

    Extracts distinct region mentions from scene_state across sessions.
    Uses a single JOIN query instead of N+1 per-session queries.
    """
    # Single query: join sessions → scenes, fetch scene_state in one pass.
    scene_result = await db.execute(
        sa.select(Scene.scene_state)
        .join(GameSession, Scene.session_id == GameSession.id)
        .where(GameSession.character_id == character_id)
    )
    scene_states = scene_result.scalars().all()

    region_visits: dict[str, int] = {}
    for scene_state in scene_states:
        if scene_state and isinstance(scene_state, dict):
            region = scene_state.get("region_id")
            if region:
                region_visits[region] = region_visits.get(region, 0) + 1

    entries = [
        JournalRegionEntry(region_id=rid, visit_count=count)
        for rid, count in sorted(region_visits.items(), key=lambda x: -x[1])
    ]
    return entries[:_MAX_REGIONS]


def _build_items_found(char: Character) -> list[str]:
    """Build 'Things I've Found' from character inventory."""
    if not char.inventory or not isinstance(char.inventory, list):
        return []

    items: list[str] = []
    for item in char.inventory:
        if isinstance(item, dict):
            item_id = item.get("item_id", item.get("id", ""))
            if item_id:
                items.append(item_id)
        elif isinstance(item, str):
            items.append(item)

    return items[:_MAX_ITEMS]


def _build_factions(char: Character) -> list[JournalFactionEntry]:
    """Build 'Factions' from character faction_standing."""
    if not char.faction_standing or not isinstance(char.faction_standing, dict):
        return []

    entries: list[JournalFactionEntry] = []
    for faction_id, standing_data in char.faction_standing.items():
        if isinstance(standing_data, dict):
            standing = standing_data.get("score", 0)
            reason = standing_data.get("reason")
        elif isinstance(standing_data, int):
            standing = standing_data
            reason = None
        else:
            continue

        entries.append(
            JournalFactionEntry(
                faction_id=faction_id,
                standing=standing,
                reason=reason,
            )
        )

    return sorted(entries, key=lambda e: -abs(e.standing))


async def _build_story_so_far(
    db: AsyncSession,
    character_id: str,
) -> list[str]:
    """Build 'Story So Far' from session summaries."""
    result = await db.execute(
        sa.select(GameSession.session_summary)
        .where(
            GameSession.character_id == character_id,
            GameSession.session_summary.is_not(None),
            GameSession.status == "ended",
        )
        .order_by(GameSession.started_at.asc())
        .limit(_MAX_SESSIONS)
    )
    return [row[0] for row in result.all() if row[0]]
