"""Tests for the consequence system models and helpers.

Covers relay/consequences/npc_state.py and relay/consequences/world_flags.py.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from relay.consequences.npc_state import (
    VALID_STATUSES,
    add_flag,
    get_instance,
    get_instances_for_character,
    get_or_create_instance,
    has_flag,
    remove_flag,
    set_disposition_override,
    update_status,
)
from relay.consequences.world_flags import (
    clear_flag,
    count_flags,
    get_flag,
    get_flags,
    set_flag,
)
from relay.consequences.world_flags import (
    has_flag as has_world_flag,
)
from relay.models import Account, Base, Character


@pytest_asyncio.fixture()
async def db():
    """In-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        # Seed an account and character
        account = Account(
            id="acct_001",
            email="test@example.com",
            password_hash="fakehash",
            tier=1,
        )
        session.add(account)
        char = Character(
            id="char_001",
            player_id="acct_001",
            world_id="inkglass_dark",
            name="Test Hero",
            level=5,
            specialisation_path_id="warrior",
            ability_scores={},
        )
        session.add(char)
        await session.commit()
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# NPC Instance State tests
# ---------------------------------------------------------------------------


class TestNpcInstanceState:
    @pytest.mark.asyncio
    async def test_get_instance_returns_none_when_untracked(self, db):
        result = await get_instance(db, "char_001", "npc_shopkeeper")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_or_create_creates_default(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_shopkeeper", world_id="inkglass_dark")
        assert instance.npc_id == "npc_shopkeeper"
        assert instance.character_id == "char_001"
        assert instance.status == "alive"
        assert instance.flags == []
        assert instance.disposition_override is None
        assert instance.hp_current is None

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self, db):
        first = await get_or_create_instance(db, "char_001", "npc_shopkeeper", world_id="inkglass_dark")
        await db.flush()
        first_id = first.id

        second = await get_or_create_instance(db, "char_001", "npc_shopkeeper", world_id="inkglass_dark")
        assert second.id == first_id

    @pytest.mark.asyncio
    async def test_get_instances_for_character(self, db):
        await get_or_create_instance(db, "char_001", "npc_a", world_id="inkglass_dark")
        await get_or_create_instance(db, "char_001", "npc_b", world_id="inkglass_dark")
        await db.flush()

        instances = await get_instances_for_character(db, "char_001")
        npc_ids = {i.npc_id for i in instances}
        assert npc_ids == {"npc_a", "npc_b"}

    @pytest.mark.asyncio
    async def test_update_status(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        old = update_status(instance, "injured", reason="attacked by player")
        assert old == "alive"
        assert instance.status == "injured"
        assert instance.last_interaction_summary == "attacked by player"

    @pytest.mark.asyncio
    async def test_update_status_invalid_raises(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        with pytest.raises(ValueError, match="Invalid NPC status"):
            update_status(instance, "exploded")

    @pytest.mark.asyncio
    async def test_all_valid_statuses(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_test", world_id="inkglass_dark")
        for status in VALID_STATUSES:
            update_status(instance, status)
            assert instance.status == status

    @pytest.mark.asyncio
    async def test_add_and_has_flag(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        assert not has_flag(instance, "attacked_by_player")
        added = add_flag(instance, "attacked_by_player")
        assert added is True
        assert has_flag(instance, "attacked_by_player")

    @pytest.mark.asyncio
    async def test_add_duplicate_flag(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        add_flag(instance, "attacked_by_player")
        added_again = add_flag(instance, "attacked_by_player")
        assert added_again is False
        assert instance.flags.count("attacked_by_player") == 1

    @pytest.mark.asyncio
    async def test_remove_flag(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        add_flag(instance, "seeking_revenge")
        removed = remove_flag(instance, "seeking_revenge")
        assert removed is True
        assert not has_flag(instance, "seeking_revenge")

    @pytest.mark.asyncio
    async def test_remove_missing_flag(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        removed = remove_flag(instance, "nonexistent")
        assert removed is False

    @pytest.mark.asyncio
    async def test_disposition_override(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        old = set_disposition_override(instance, -80)
        assert old is None
        assert instance.disposition_override == -80

    @pytest.mark.asyncio
    async def test_disposition_override_clamped(self, db):
        instance = await get_or_create_instance(db, "char_001", "npc_guard", world_id="inkglass_dark")
        set_disposition_override(instance, -200)
        assert instance.disposition_override == -100
        set_disposition_override(instance, 999)
        assert instance.disposition_override == 100


# ---------------------------------------------------------------------------
# World Flags tests
# ---------------------------------------------------------------------------


class TestWorldFlags:
    @pytest.mark.asyncio
    async def test_get_flag_returns_none_when_unset(self, db):
        result = await get_flag(db, "char_001", "bounty_active")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_and_get_flag(self, db):
        await set_flag(
            db,
            "char_001",
            "wanted_in:market_district",
            reason="witnessed theft",
            source="consequence",
        )
        await db.flush()

        loaded = await get_flag(db, "char_001", "wanted_in:market_district")
        assert loaded is not None
        assert loaded.value == "true"
        assert loaded.reason == "witnessed theft"
        assert loaded.source == "consequence"

    @pytest.mark.asyncio
    async def test_set_flag_upsert(self, db):
        first = await set_flag(
            db,
            "char_001",
            "bounty_active",
            value="500",
            reason="murder",
        )
        await db.flush()
        first_id = first.id

        second = await set_flag(
            db,
            "char_001",
            "bounty_active",
            value="1000",
            reason="second murder",
        )
        # Same row, updated
        assert second.id == first_id
        assert second.value == "1000"
        assert second.reason == "second murder"

    @pytest.mark.asyncio
    async def test_has_flag(self, db):
        assert not await has_world_flag(db, "char_001", "bounty_active")
        await set_flag(db, "char_001", "bounty_active")
        await db.flush()
        assert await has_world_flag(db, "char_001", "bounty_active")

    @pytest.mark.asyncio
    async def test_clear_flag(self, db):
        await set_flag(db, "char_001", "bounty_active")
        await db.flush()

        cleared = await clear_flag(db, "char_001", "bounty_active")
        assert cleared is True
        assert not await has_world_flag(db, "char_001", "bounty_active")

    @pytest.mark.asyncio
    async def test_clear_missing_flag(self, db):
        cleared = await clear_flag(db, "char_001", "nonexistent")
        assert cleared is False

    @pytest.mark.asyncio
    async def test_get_flags_all(self, db):
        await set_flag(db, "char_001", "wanted_in:market_district")
        await set_flag(db, "char_001", "bounty_active")
        await set_flag(db, "char_001", "wanted_in:docks")
        await db.flush()

        all_flags = await get_flags(db, "char_001")
        assert len(all_flags) == 3

    @pytest.mark.asyncio
    async def test_get_flags_prefix_filter(self, db):
        await set_flag(db, "char_001", "wanted_in:market_district")
        await set_flag(db, "char_001", "bounty_active")
        await set_flag(db, "char_001", "wanted_in:docks")
        await db.flush()

        wanted = await get_flags(db, "char_001", prefix="wanted_in:")
        assert len(wanted) == 2
        flag_names = {f.flag for f in wanted}
        assert flag_names == {"wanted_in:market_district", "wanted_in:docks"}

    @pytest.mark.asyncio
    async def test_count_flags(self, db):
        await set_flag(db, "char_001", "npc_killed:guard_01")
        await set_flag(db, "char_001", "npc_killed:merchant_03")
        await set_flag(db, "char_001", "npc_killed:civilian_07")
        await set_flag(db, "char_001", "bounty_active")
        await db.flush()

        killed = await count_flags(db, "char_001", "npc_killed:")
        assert killed == 3
