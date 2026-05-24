"""Tests for narrative thread tracking — Layer 2 of §6 Three-Layer Narrative.

Covers relay/narrative/threads.py (validation + persistence) and the
narrative_signals field in _ANALYSIS_TOOL.
"""

from __future__ import annotations

import pytest

from relay.narrative.threads import (
    _MAX_SIGNALS_PER_TURN,
    _MAX_SUMMARY_LENGTH,
    _THREAD_KEY_RE,
    VALID_SIGNAL_TYPES,
    validate_narrative_signals,
)

# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateNarrativeSignals:
    def _valid_signal(self, **overrides):
        base = {
            "type": "commitment",
            "summary": "Player promised to find the dock worker's brother",
            "thread_key": "missing_brother",
            "related_npcs": ["dock_worker_03"],
            "related_regions": ["docks"],
        }
        base.update(overrides)
        return base

    def test_valid_signal_passes(self):
        result = validate_narrative_signals([self._valid_signal()])
        assert len(result) == 1
        assert result[0]["type"] == "commitment"
        assert result[0]["thread_key"] == "missing_brother"

    def test_all_signal_types_valid(self):
        for signal_type in VALID_SIGNAL_TYPES:
            result = validate_narrative_signals([self._valid_signal(type=signal_type)])
            assert len(result) == 1
            assert result[0]["type"] == signal_type

    def test_invalid_type_rejected(self):
        result = validate_narrative_signals([self._valid_signal(type="curiosity")])
        assert len(result) == 0

    def test_missing_type_rejected(self):
        sig = self._valid_signal()
        del sig["type"]
        result = validate_narrative_signals([sig])
        assert len(result) == 0

    def test_missing_summary_rejected(self):
        sig = self._valid_signal()
        del sig["summary"]
        result = validate_narrative_signals([sig])
        assert len(result) == 0

    def test_empty_summary_rejected(self):
        result = validate_narrative_signals([self._valid_signal(summary="")])
        assert len(result) == 0

    def test_whitespace_only_summary_rejected(self):
        result = validate_narrative_signals([self._valid_signal(summary="   ")])
        assert len(result) == 0

    def test_missing_thread_key_rejected(self):
        sig = self._valid_signal()
        del sig["thread_key"]
        result = validate_narrative_signals([sig])
        assert len(result) == 0

    def test_uppercase_thread_key_normalised(self):
        # Uppercase is normalised to lowercase before validation
        result = validate_narrative_signals([self._valid_signal(thread_key="MissingBrother")])
        assert len(result) == 1
        assert result[0]["thread_key"] == "missingbrother"

    def test_single_char_thread_key_rejected(self):
        result = validate_narrative_signals([self._valid_signal(thread_key="a")])
        assert len(result) == 0

    def test_thread_key_with_spaces_rejected(self):
        result = validate_narrative_signals([self._valid_signal(thread_key="missing brother")])
        assert len(result) == 0

    def test_thread_key_starting_with_number_rejected(self):
        result = validate_narrative_signals([self._valid_signal(thread_key="3missing")])
        assert len(result) == 0

    def test_valid_thread_key_formats(self):
        valid_keys = ["missing_brother", "guild_corruption", "a_b", "thread_42"]
        for key in valid_keys:
            result = validate_narrative_signals([self._valid_signal(thread_key=key)])
            assert len(result) == 1, f"Key {key!r} should be valid"

    def test_summary_truncated_to_max_length(self):
        long_summary = "x" * (_MAX_SUMMARY_LENGTH + 100)
        result = validate_narrative_signals([self._valid_signal(summary=long_summary)])
        assert len(result) == 1
        assert len(result[0]["summary"]) == _MAX_SUMMARY_LENGTH

    def test_capped_at_max_per_turn(self):
        signals = [self._valid_signal(thread_key=f"thread_{i}") for i in range(_MAX_SIGNALS_PER_TURN + 5)]
        result = validate_narrative_signals(signals)
        assert len(result) == _MAX_SIGNALS_PER_TURN

    def test_related_npcs_optional(self):
        sig = self._valid_signal()
        del sig["related_npcs"]
        result = validate_narrative_signals([sig])
        assert len(result) == 1
        assert result[0]["related_npcs"] == []

    def test_related_regions_optional(self):
        sig = self._valid_signal()
        del sig["related_regions"]
        result = validate_narrative_signals([sig])
        assert len(result) == 1
        assert result[0]["related_regions"] == []

    def test_related_npcs_capped(self):
        sig = self._valid_signal(related_npcs=[f"npc_{i}" for i in range(20)])
        result = validate_narrative_signals([sig])
        assert len(result[0]["related_npcs"]) <= 5

    def test_non_string_related_npcs_filtered(self):
        sig = self._valid_signal(related_npcs=[123, None, "valid_npc"])
        result = validate_narrative_signals([sig])
        assert result[0]["related_npcs"] == ["valid_npc"]

    def test_non_list_input_returns_empty(self):
        result = validate_narrative_signals("not a list")
        assert result == []

    def test_non_dict_items_skipped(self):
        result = validate_narrative_signals(["not_a_dict", self._valid_signal()])
        assert len(result) == 1

    def test_empty_list_returns_empty(self):
        result = validate_narrative_signals([])
        assert result == []

    def test_mixed_valid_and_invalid(self):
        signals = [
            self._valid_signal(thread_key="good_thread"),
            self._valid_signal(type="invalid_type"),  # bad type
            self._valid_signal(thread_key="another_good"),
        ]
        result = validate_narrative_signals(signals)
        assert len(result) == 2
        assert result[0]["thread_key"] == "good_thread"
        assert result[1]["thread_key"] == "another_good"

    def test_thread_key_normalised_to_lowercase(self):
        result = validate_narrative_signals([self._valid_signal(thread_key="MISSING_BROTHER")])
        # Uppercase is normalised before regex check
        assert len(result) == 1
        assert result[0]["thread_key"] == "missing_brother"


# ---------------------------------------------------------------------------
# Thread key regex tests
# ---------------------------------------------------------------------------


class TestThreadKeyRegex:
    def test_valid_keys(self):
        valid = [
            "missing_brother",
            "guild_corruption",
            "ab",
            "a_b",
            "thread_42",
            "dark_secret_of_the_docks",
        ]
        for key in valid:
            assert _THREAD_KEY_RE.match(key), f"{key!r} should match"

    def test_invalid_keys(self):
        invalid = [
            "a",  # too short
            "_leading",  # starts with underscore
            "trailing_",  # ends with underscore
            "has spaces",
            "MixedCase",
            "123start",  # starts with number
            "",
        ]
        for key in invalid:
            assert not _THREAD_KEY_RE.match(key), f"{key!r} should not match"


# ---------------------------------------------------------------------------
# Persistence tests (using in-memory DB)
# ---------------------------------------------------------------------------


class TestUpsertThread:
    @pytest.fixture
    async def db(self):
        """Provide a fresh in-memory database session."""
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from relay.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

        await engine.dispose()

    def _signal(self, **overrides):
        base = {
            "type": "commitment",
            "summary": "Player promised to find the dock worker's brother",
            "thread_key": "missing_brother",
            "related_npcs": ["dock_worker_03"],
            "related_regions": ["docks"],
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_create_new_thread(self, db):
        from relay.narrative.threads import upsert_thread

        thread = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(),
            session_id="sess_1",
        )
        assert thread.thread_key == "missing_brother"
        assert thread.signal_type == "commitment"
        assert thread.mention_count == 1
        assert thread.status == "active"
        assert thread.first_seen_session_id == "sess_1"
        assert thread.last_seen_session_id == "sess_1"
        assert thread.related_npcs == ["dock_worker_03"]

    @pytest.mark.asyncio
    async def test_upsert_increments_mention_count(self, db):
        from relay.narrative.threads import upsert_thread

        thread1 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(),
            session_id="sess_1",
        )
        await db.flush()

        thread2 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(summary="Player asked again about the brother"),
            session_id="sess_2",
        )
        assert thread2.id == thread1.id
        assert thread2.mention_count == 2
        assert thread2.summary == "Player asked again about the brother"
        assert thread2.last_seen_session_id == "sess_2"

    @pytest.mark.asyncio
    async def test_upsert_merges_related_npcs(self, db):
        from relay.narrative.threads import upsert_thread

        await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(related_npcs=["npc_a"]),
        )
        await db.flush()

        thread = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(related_npcs=["npc_b"]),
        )
        assert "npc_a" in thread.related_npcs
        assert "npc_b" in thread.related_npcs

    @pytest.mark.asyncio
    async def test_upsert_reactivates_dormant(self, db):
        from relay.narrative.threads import upsert_thread

        thread = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(),
        )
        thread.status = "dormant"
        await db.flush()

        reactivated = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(),
        )
        assert reactivated.status == "active"

    @pytest.mark.asyncio
    async def test_different_characters_get_separate_threads(self, db):
        from relay.narrative.threads import upsert_thread

        t1 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="inkglass_dark",
            signal=self._signal(),
        )
        await db.flush()

        t2 = await upsert_thread(
            db,
            character_id="char_2",
            world_id="inkglass_dark",
            signal=self._signal(),
        )
        assert t1.id != t2.id
        assert t1.mention_count == 1
        assert t2.mention_count == 1


class TestGetActiveThreads:
    @pytest.fixture
    async def db(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from relay.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_returns_active_only(self, db):
        from relay.narrative.threads import get_active_threads, upsert_thread

        await upsert_thread(
            db,
            character_id="char_1",
            world_id="w",
            signal={"type": "interest", "summary": "active", "thread_key": "active_thread"},
        )
        t2 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="w",
            signal={"type": "interest", "summary": "resolved", "thread_key": "resolved_thread"},
        )
        t2.status = "resolved"
        await db.flush()

        threads = await get_active_threads(db, "char_1")
        assert len(threads) == 1
        assert threads[0].thread_key == "active_thread"

    @pytest.mark.asyncio
    async def test_ordered_by_mention_count(self, db):
        from relay.narrative.threads import get_active_threads, upsert_thread

        t1 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="w",
            signal={"type": "interest", "summary": "few mentions", "thread_key": "thread_a"},
        )
        t1.mention_count = 2

        t2 = await upsert_thread(
            db,
            character_id="char_1",
            world_id="w",
            signal={"type": "commitment", "summary": "many mentions", "thread_key": "thread_b"},
        )
        t2.mention_count = 10
        await db.flush()

        threads = await get_active_threads(db, "char_1")
        assert len(threads) == 2
        assert threads[0].thread_key == "thread_b"  # More mentions first

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        from relay.narrative.threads import get_active_threads, upsert_thread

        for i in range(15):
            await upsert_thread(
                db,
                character_id="char_1",
                world_id="w",
                signal={"type": "interest", "summary": f"thread {i}", "thread_key": f"thread_{i:02d}"},
            )
        await db.flush()

        threads = await get_active_threads(db, "char_1", limit=5)
        assert len(threads) == 5

    @pytest.mark.asyncio
    async def test_empty_for_unknown_character(self, db):
        from relay.narrative.threads import get_active_threads

        threads = await get_active_threads(db, "nonexistent")
        assert threads == []


class TestResolveThread:
    @pytest.fixture
    async def db(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from relay.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_resolves_existing_thread(self, db):
        from relay.narrative.threads import resolve_thread, upsert_thread

        await upsert_thread(
            db,
            character_id="char_1",
            world_id="w",
            signal={"type": "interest", "summary": "test", "thread_key": "my_thread"},
        )
        await db.flush()

        result = await resolve_thread(db, "char_1", "my_thread")
        assert result is True

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_returns_false(self, db):
        from relay.narrative.threads import resolve_thread

        result = await resolve_thread(db, "char_1", "no_such_thread")
        assert result is False


# ---------------------------------------------------------------------------
# GameStateContext integration tests
# ---------------------------------------------------------------------------


class TestNarrativeThreadsInContext:
    def test_threads_included_in_context_block(self):
        from relay.ai.game_context import GameStateContext, format_context_block

        ctx = GameStateContext(
            active_narrative_threads=[
                {
                    "thread_key": "missing_brother",
                    "type": "commitment",
                    "summary": "Player promised to find the brother",
                    "mentions": 3,
                    "related_npcs": ["dock_worker"],
                    "related_regions": ["docks"],
                },
            ],
        )
        block = format_context_block(ctx, "Test NPC")
        assert "NARRATIVE THREADS" in block
        assert "missing_brother" in block
        assert "commitment" in block
        assert "3x" in block

    def test_empty_threads_not_in_context(self):
        from relay.ai.game_context import GameStateContext, format_context_block

        ctx = GameStateContext(active_narrative_threads=[])
        block = format_context_block(ctx, "Test NPC")
        assert "NARRATIVE THREADS" not in block

    def test_threads_capped_at_five_in_format(self):
        from relay.ai.game_context import GameStateContext, format_context_block

        threads = [
            {
                "thread_key": f"thread_{i}",
                "type": "interest",
                "summary": f"Thread {i}",
                "mentions": i,
                "related_npcs": [],
                "related_regions": [],
            }
            for i in range(10)
        ]
        ctx = GameStateContext(active_narrative_threads=threads)
        block = format_context_block(ctx, "Test NPC")
        # Should only show first 5
        assert "thread_0" in block
        assert "thread_4" in block
        assert "thread_5" not in block


# ---------------------------------------------------------------------------
# _ANALYSIS_TOOL schema test
# ---------------------------------------------------------------------------


class TestAnalysisToolNarrativeSignals:
    def test_narrative_signals_in_schema(self):
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        props = _ANALYSIS_TOOL["input_schema"]["properties"]
        assert "narrative_signals" in props
        ns = props["narrative_signals"]
        assert ns["type"] == "array"
        items = ns["items"]
        assert "type" in items["properties"]
        assert "summary" in items["properties"]
        assert "thread_key" in items["properties"]
        assert items["required"] == ["type", "summary", "thread_key"]

    def test_narrative_signals_not_required(self):
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        required = _ANALYSIS_TOOL["input_schema"]["required"]
        assert "narrative_signals" not in required

    def test_signal_type_enum(self):
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        props = _ANALYSIS_TOOL["input_schema"]["properties"]
        type_enum = props["narrative_signals"]["items"]["properties"]["type"]["enum"]
        assert set(type_enum) == {"commitment", "interest", "revelation", "tension"}
