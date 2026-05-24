"""Tests for World Journal (§2) and Starting Scenario Preferences (§3).

Covers:
- Journal Pydantic models (JournalNpcEntry, JournalRegionEntry, etc.)
- WorldJournal aggregation (build_journal + helper functions)
- ScenarioPreferences validation and defaults
- CharacterPreferences ORM model
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from relay.journal.aggregator import (
    _build_factions,
    _build_items_found,
    _build_places_visited,
    _build_story_so_far,
    build_journal,
)
from relay.models import (
    Account,
    Base,
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
    ScenarioPreferences,
    WorldJournal,
)

# ---------------------------------------------------------------------------
# Async DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db_session():
    """Provide a fresh in-memory async SQLite session for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _make_account(account_id: str = "acct_001") -> Account:
    return Account(
        id=account_id,
        email=f"{account_id}@test.com",
        password_hash="fakehash",
        tier=1,
    )


def _make_character(
    character_id: str = "char_001",
    player_id: str = "acct_001",
    *,
    relationships: dict | None = None,
    inventory: list | None = None,
    faction_standing: dict | None = None,
) -> Character:
    return Character(
        id=character_id,
        player_id=player_id,
        world_id="inkglass_dark",
        name="Test Hero",
        specialisation_path_id="warrior",
        ability_scores={
            "strength": 14,
            "dexterity": 12,
            "constitution": 13,
            "intelligence": 10,
            "wisdom": 11,
            "charisma": 10,
        },
        hp_current=20,
        hp_max=20,
        relationships=relationships or {},
        inventory=inventory or [],
        faction_standing=faction_standing or {},
    )


# ===========================================================================
# §2 — Journal Pydantic Models
# ===========================================================================


class TestJournalNpcEntry:
    def test_defaults(self):
        entry = JournalNpcEntry(npc_id="npc_1")
        assert entry.npc_id == "npc_1"
        assert entry.name is None
        assert entry.relationship_score == 0
        assert entry.last_scene_summary is None
        assert entry.player_notes is None

    def test_full(self):
        entry = JournalNpcEntry(
            npc_id="npc_1",
            name="Seta",
            relationship_score=15,
            last_scene_summary="Traded herbs.",
            player_notes="Friendly herbalist in market.",
        )
        assert entry.relationship_score == 15
        assert entry.player_notes == "Friendly herbalist in market."

    def test_negative_score(self):
        entry = JournalNpcEntry(npc_id="npc_x", relationship_score=-30)
        assert entry.relationship_score == -30


class TestJournalRegionEntry:
    def test_defaults(self):
        entry = JournalRegionEntry(region_id="market")
        assert entry.visit_count == 1
        assert entry.summary is None

    def test_custom_visit_count(self):
        entry = JournalRegionEntry(region_id="forest", visit_count=7, summary="Dense and dark.")
        assert entry.visit_count == 7
        assert entry.summary == "Dense and dark."


class TestJournalFactionEntry:
    def test_defaults(self):
        entry = JournalFactionEntry(faction_id="guild_merchants")
        assert entry.standing == 0
        assert entry.reason is None

    def test_with_reason(self):
        entry = JournalFactionEntry(
            faction_id="guild_thieves",
            standing=-25,
            reason="Exposed their smuggling operation.",
        )
        assert entry.standing == -25
        assert entry.reason == "Exposed their smuggling operation."


class TestWorldJournal:
    def test_empty_defaults(self):
        journal = WorldJournal(character_id="c1", world_id="inkglass_dark")
        assert journal.people_met == []
        assert journal.places_visited == []
        assert journal.items_found == []
        assert journal.factions == []
        assert journal.story_so_far == []

    def test_populated(self):
        journal = WorldJournal(
            character_id="c1",
            world_id="inkglass_dark",
            people_met=[JournalNpcEntry(npc_id="npc_1")],
            places_visited=[JournalRegionEntry(region_id="market")],
            items_found=["sword_iron", "potion_healing"],
            factions=[JournalFactionEntry(faction_id="guild_merchants", standing=10)],
            story_so_far=["Chapter one summary.", "Chapter two summary."],
        )
        assert len(journal.people_met) == 1
        assert len(journal.items_found) == 2
        assert len(journal.story_so_far) == 2


# ===========================================================================
# §3 — ScenarioPreferences Pydantic Model
# ===========================================================================


class TestScenarioPreferences:
    def test_all_defaults(self):
        prefs = ScenarioPreferences()
        assert prefs.backstory_blurb == ""
        assert prefs.story_interests == []
        assert prefs.topics_to_avoid == []
        assert prefs.content_rating == "moderate"
        assert prefs.narrative_pace == "moderate"
        assert prefs.companion_interest == "moderate"
        assert prefs.exploration_style == "balanced"

    def test_full_custom(self):
        prefs = ScenarioPreferences(
            backstory_blurb="A wandering knight seeking redemption.",
            story_interests=["mystery", "personal_drama"],
            topics_to_avoid=["spiders", "claustrophobia"],
            content_rating="mature",
            narrative_pace="intense",
            companion_interest="high",
            exploration_style="freeform",
        )
        assert prefs.content_rating == "mature"
        assert prefs.narrative_pace == "intense"
        assert prefs.companion_interest == "high"
        assert prefs.exploration_style == "freeform"
        assert "mystery" in prefs.story_interests

    def test_backstory_max_length(self):
        # Exactly 2000 chars — should pass
        prefs = ScenarioPreferences(backstory_blurb="a" * 2000)
        assert len(prefs.backstory_blurb) == 2000

    def test_backstory_too_long(self):
        with pytest.raises(ValidationError, match="backstory_blurb"):
            ScenarioPreferences(backstory_blurb="a" * 2001)

    def test_invalid_content_rating(self):
        with pytest.raises(ValidationError, match="content_rating"):
            ScenarioPreferences(content_rating="explicit")

    def test_invalid_narrative_pace(self):
        with pytest.raises(ValidationError, match="narrative_pace"):
            ScenarioPreferences(narrative_pace="frantic")

    def test_invalid_companion_interest(self):
        with pytest.raises(ValidationError, match="companion_interest"):
            ScenarioPreferences(companion_interest="none")

    def test_invalid_exploration_style(self):
        with pytest.raises(ValidationError, match="exploration_style"):
            ScenarioPreferences(exploration_style="railroaded")

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScenarioPreferences(unknown_field="value")

    def test_relaxed_pace(self):
        prefs = ScenarioPreferences(narrative_pace="relaxed")
        assert prefs.narrative_pace == "relaxed"

    def test_low_companion_interest(self):
        prefs = ScenarioPreferences(companion_interest="low")
        assert prefs.companion_interest == "low"

    def test_guided_exploration(self):
        prefs = ScenarioPreferences(exploration_style="guided")
        assert prefs.exploration_style == "guided"


# ===========================================================================
# §3 — CharacterPreferences ORM Model
# ===========================================================================


class TestCharacterPreferencesORM:
    @pytest.mark.asyncio
    async def test_create_and_read(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        prefs = CharacterPreferences(
            id="pref_001",
            character_id="char_001",
            world_id="inkglass_dark",
            backstory_blurb="A wandering knight.",
            story_interests=["mystery", "combat"],
            topics_to_avoid=["horror"],
            content_rating="mature",
            narrative_pace="intense",
            companion_interest="high",
            exploration_style="freeform",
            npc_notes={"npc_1": "Friendly merchant"},
        )
        db_session.add(prefs)
        await db_session.flush()

        import sqlalchemy as sa

        result = await db_session.execute(
            sa.select(CharacterPreferences).where(CharacterPreferences.character_id == "char_001")
        )
        loaded = result.scalar_one()
        assert loaded.backstory_blurb == "A wandering knight."
        assert loaded.content_rating == "mature"
        assert loaded.npc_notes == {"npc_1": "Friendly merchant"}
        assert loaded.story_interests == ["mystery", "combat"]

    @pytest.mark.asyncio
    async def test_defaults(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        prefs = CharacterPreferences(
            id="pref_002",
            character_id="char_001",
            world_id="inkglass_dark",
        )
        db_session.add(prefs)
        await db_session.flush()

        import sqlalchemy as sa

        result = await db_session.execute(sa.select(CharacterPreferences).where(CharacterPreferences.id == "pref_002"))
        loaded = result.scalar_one()
        assert loaded.backstory_blurb == ""
        assert loaded.content_rating == "moderate"
        assert loaded.narrative_pace == "moderate"
        assert loaded.companion_interest == "moderate"
        assert loaded.exploration_style == "balanced"
        assert loaded.npc_notes == {}
        assert loaded.story_interests == []
        assert loaded.topics_to_avoid == []


# ===========================================================================
# §2 — Journal Aggregator (build_journal + helpers)
# ===========================================================================


class TestBuildItemsFound:
    """Unit tests for _build_items_found (no DB required)."""

    def test_empty_inventory(self):
        char = _make_character(inventory=[])
        assert _build_items_found(char) == []

    def test_string_items(self):
        char = _make_character(inventory=["sword_iron", "shield_wood", "potion_healing"])
        items = _build_items_found(char)
        assert items == ["sword_iron", "shield_wood", "potion_healing"]

    def test_dict_items_with_item_id(self):
        char = _make_character(
            inventory=[
                {"item_id": "sword_iron", "quantity": 1},
                {"item_id": "potion_healing", "quantity": 3},
            ]
        )
        items = _build_items_found(char)
        assert items == ["sword_iron", "potion_healing"]

    def test_dict_items_with_id_fallback(self):
        char = _make_character(inventory=[{"id": "gem_ruby", "quantity": 1}])
        items = _build_items_found(char)
        assert items == ["gem_ruby"]

    def test_mixed_inventory(self):
        char = _make_character(
            inventory=[
                "raw_string_item",
                {"item_id": "dict_item", "quantity": 1},
            ]
        )
        items = _build_items_found(char)
        assert items == ["raw_string_item", "dict_item"]

    def test_none_inventory(self):
        char = _make_character()
        char.inventory = None
        assert _build_items_found(char) == []

    def test_max_items_cap(self):
        char = _make_character(inventory=[f"item_{i}" for i in range(150)])
        items = _build_items_found(char)
        assert len(items) == 100  # _MAX_ITEMS


class TestBuildFactions:
    """Unit tests for _build_factions (no DB required)."""

    def test_empty_factions(self):
        char = _make_character(faction_standing={})
        assert _build_factions(char) == []

    def test_integer_standings(self):
        char = _make_character(faction_standing={"guild_merchants": 25, "guild_thieves": -40})
        factions = _build_factions(char)
        # Sorted by abs(standing) descending
        assert factions[0].faction_id == "guild_thieves"
        assert factions[0].standing == -40
        assert factions[1].faction_id == "guild_merchants"
        assert factions[1].standing == 25

    def test_dict_standings(self):
        char = _make_character(
            faction_standing={
                "guard": {"score": 15, "reason": "Helped patrol"},
                "bandit": {"score": -30},
            }
        )
        factions = _build_factions(char)
        assert len(factions) == 2
        guard = next(f for f in factions if f.faction_id == "guard")
        assert guard.standing == 15
        assert guard.reason == "Helped patrol"
        bandit = next(f for f in factions if f.faction_id == "bandit")
        assert bandit.standing == -30
        assert bandit.reason is None

    def test_none_faction_standing(self):
        char = _make_character()
        char.faction_standing = None
        assert _build_factions(char) == []

    def test_invalid_standing_skipped(self):
        char = _make_character(faction_standing={"valid": 10, "invalid": "not_a_number_or_dict"})
        factions = _build_factions(char)
        assert len(factions) == 1
        assert factions[0].faction_id == "valid"


class TestBuildStoryFar:
    """Async tests for _build_story_so_far."""

    @pytest.mark.asyncio
    async def test_no_sessions(self, db_session: AsyncSession):
        stories = await _build_story_so_far(db_session, "char_nonexistent")
        assert stories == []

    @pytest.mark.asyncio
    async def test_ended_sessions_with_summaries(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        for i in range(3):
            session = GameSession(
                id=f"sess_{i}",
                player_id="acct_001",
                character_id="char_001",
                world_id="inkglass_dark",
                status="ended",
                session_summary=f"Chapter {i + 1} summary.",
            )
            db_session.add(session)
        await db_session.flush()

        stories = await _build_story_so_far(db_session, "char_001")
        assert len(stories) == 3
        assert stories[0] == "Chapter 1 summary."
        assert stories[2] == "Chapter 3 summary."

    @pytest.mark.asyncio
    async def test_active_sessions_excluded(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        db_session.add(
            GameSession(
                id="sess_active",
                player_id="acct_001",
                character_id="char_001",
                world_id="inkglass_dark",
                status="active",
                session_summary="Should be excluded.",
            )
        )
        db_session.add(
            GameSession(
                id="sess_ended",
                player_id="acct_001",
                character_id="char_001",
                world_id="inkglass_dark",
                status="ended",
                session_summary="Should be included.",
            )
        )
        await db_session.flush()

        stories = await _build_story_so_far(db_session, "char_001")
        assert len(stories) == 1
        assert stories[0] == "Should be included."

    @pytest.mark.asyncio
    async def test_null_summaries_excluded(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        db_session.add(
            GameSession(
                id="sess_no_summary",
                player_id="acct_001",
                character_id="char_001",
                world_id="inkglass_dark",
                status="ended",
                session_summary=None,
            )
        )
        await db_session.flush()

        stories = await _build_story_so_far(db_session, "char_001")
        assert stories == []


class TestBuildPlacesVisited:
    """Async tests for _build_places_visited."""

    @pytest.mark.asyncio
    async def test_no_sessions(self, db_session: AsyncSession):
        places = await _build_places_visited(db_session, "char_nonexistent")
        assert places == []

    @pytest.mark.asyncio
    async def test_region_from_scene_state(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        session = GameSession(
            id="sess_1",
            player_id="acct_001",
            character_id="char_001",
            world_id="inkglass_dark",
            status="ended",
        )
        db_session.add(session)
        await db_session.flush()

        db_session.add(
            Scene(
                id="scene_1",
                session_id="sess_1",
                npc_id="npc_1",
                scene_state={"region_id": "market"},
            )
        )
        db_session.add(
            Scene(
                id="scene_2",
                session_id="sess_1",
                npc_id="npc_2",
                scene_state={"region_id": "market"},
            )
        )
        db_session.add(
            Scene(
                id="scene_3",
                session_id="sess_1",
                npc_id="npc_3",
                scene_state={"region_id": "forest"},
            )
        )
        await db_session.flush()

        places = await _build_places_visited(db_session, "char_001")
        assert len(places) == 2
        # Sorted by visit count descending
        assert places[0].region_id == "market"
        assert places[0].visit_count == 2
        assert places[1].region_id == "forest"
        assert places[1].visit_count == 1

    @pytest.mark.asyncio
    async def test_scene_without_region_id(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        session = GameSession(
            id="sess_1",
            player_id="acct_001",
            character_id="char_001",
            world_id="inkglass_dark",
            status="ended",
        )
        db_session.add(session)
        await db_session.flush()

        db_session.add(
            Scene(
                id="scene_1",
                session_id="sess_1",
                npc_id="npc_1",
                scene_state={"weather": "rain"},  # No region_id
            )
        )
        await db_session.flush()

        places = await _build_places_visited(db_session, "char_001")
        assert places == []


class TestBuildPeopleMet:
    """Async tests for the people_met section via build_journal."""

    @pytest.mark.asyncio
    async def test_npc_from_instance_state(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character(relationships={"npc_1": {"score": 10}})
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        nis = NpcInstanceState(
            id="nis_1",
            character_id="char_001",
            npc_id="npc_1",
            world_id="inkglass_dark",
            last_interaction_summary="Traded goods at the market.",
        )
        db_session.add(nis)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        assert len(journal.people_met) == 1
        assert journal.people_met[0].npc_id == "npc_1"
        assert journal.people_met[0].relationship_score == 10
        assert journal.people_met[0].last_scene_summary == "Traded goods at the market."

    @pytest.mark.asyncio
    async def test_npc_from_relationships_only(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character(relationships={"npc_rel_only": 5})
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        assert len(journal.people_met) == 1
        assert journal.people_met[0].npc_id == "npc_rel_only"
        assert journal.people_met[0].relationship_score == 5

    @pytest.mark.asyncio
    async def test_npc_with_player_notes(self, db_session: AsyncSession):
        acct = _make_account()
        char = _make_character(relationships={"npc_1": 0})
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        prefs = CharacterPreferences(
            id="pref_notes",
            character_id="char_001",
            world_id="inkglass_dark",
            npc_notes={"npc_1": "Sells rare herbs on Thursdays."},
        )
        db_session.add(prefs)

        nis = NpcInstanceState(
            id="nis_1",
            character_id="char_001",
            npc_id="npc_1",
            world_id="inkglass_dark",
        )
        db_session.add(nis)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        assert journal.people_met[0].player_notes == "Sells rare herbs on Thursdays."

    @pytest.mark.asyncio
    async def test_no_duplicate_npcs(self, db_session: AsyncSession):
        """NPCs in both instance_state AND relationships should appear only once."""
        acct = _make_account()
        char = _make_character(relationships={"npc_1": {"score": 10}})
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        nis = NpcInstanceState(
            id="nis_1",
            character_id="char_001",
            npc_id="npc_1",
            world_id="inkglass_dark",
        )
        db_session.add(nis)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        npc_ids = [p.npc_id for p in journal.people_met]
        assert npc_ids.count("npc_1") == 1


class TestBuildJournalIntegration:
    """Integration tests for the full build_journal function."""

    @pytest.mark.asyncio
    async def test_nonexistent_character(self, db_session: AsyncSession):
        journal = await build_journal(db_session, "nonexistent", "inkglass_dark")
        assert journal.character_id == "nonexistent"
        assert journal.people_met == []
        assert journal.items_found == []
        assert journal.factions == []

    @pytest.mark.asyncio
    async def test_full_journal(self, db_session: AsyncSession):
        """Build a journal with data in every section."""
        acct = _make_account()
        char = _make_character(
            relationships={"npc_1": {"score": 10}, "npc_2": -5},
            inventory=[
                {"item_id": "sword_iron", "quantity": 1},
                "potion_healing",
            ],
            faction_standing={
                "guild_merchants": 20,
                "bandits": {"score": -50, "reason": "Destroyed camp"},
            },
        )
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        # NPC instance state
        nis = NpcInstanceState(
            id="nis_1",
            character_id="char_001",
            npc_id="npc_1",
            world_id="inkglass_dark",
            last_interaction_summary="Helped with herb delivery.",
        )
        db_session.add(nis)

        # Session with summary
        session = GameSession(
            id="sess_1",
            player_id="acct_001",
            character_id="char_001",
            world_id="inkglass_dark",
            status="ended",
            session_summary="Hero arrived in the market district.",
        )
        db_session.add(session)
        await db_session.flush()

        # Scene with region
        scene = Scene(
            id="scene_1",
            session_id="sess_1",
            npc_id="npc_1",
            scene_state={"region_id": "market"},
        )
        db_session.add(scene)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")

        # People
        assert len(journal.people_met) == 2
        npc_ids = {p.npc_id for p in journal.people_met}
        assert "npc_1" in npc_ids
        assert "npc_2" in npc_ids

        # Places
        assert len(journal.places_visited) == 1
        assert journal.places_visited[0].region_id == "market"

        # Items
        assert journal.items_found == ["sword_iron", "potion_healing"]

        # Factions (sorted by abs standing)
        assert len(journal.factions) == 2
        assert journal.factions[0].faction_id == "bandits"
        assert journal.factions[0].standing == -50

        # Story
        assert len(journal.story_so_far) == 1
        assert journal.story_so_far[0] == "Hero arrived in the market district."

    @pytest.mark.asyncio
    async def test_empty_character(self, db_session: AsyncSession):
        """Character with no interactions has empty journal."""
        acct = _make_account()
        char = _make_character()
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        assert journal.people_met == []
        assert journal.places_visited == []
        assert journal.items_found == []
        assert journal.factions == []
        assert journal.story_so_far == []

    @pytest.mark.asyncio
    async def test_integer_relationship_in_dict(self, db_session: AsyncSession):
        """Relationships stored as int (not dict) should still work."""
        acct = _make_account()
        char = _make_character(relationships={"npc_x": 42})
        db_session.add(acct)
        db_session.add(char)
        await db_session.flush()

        nis = NpcInstanceState(
            id="nis_x",
            character_id="char_001",
            npc_id="npc_x",
            world_id="inkglass_dark",
        )
        db_session.add(nis)
        await db_session.flush()

        journal = await build_journal(db_session, "char_001", "inkglass_dark")
        assert journal.people_met[0].relationship_score == 42
