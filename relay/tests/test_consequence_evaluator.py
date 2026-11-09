"""Tests for the consequence evaluator — world mutation validation and application.

Covers relay/consequences/evaluator.py: validate_world_mutations and
apply_world_mutations, plus the new mutation models and state log helpers.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from relay.consequences.evaluator import (
    _MAX_MUTATIONS_PER_TURN,
    MAX_NEGATIVE_DELTA,
    MAX_POSITIVE_DELTA_PER_TURN,
    MAX_POSITIVE_FACTION_PER_SESSION,
    MAX_POSITIVE_RELATIONSHIP_PER_SESSION,
    apply_session_gain_caps,
    apply_world_mutations,
    constrain_world_mutations,
    validate_world_mutations,
)
from relay.models import Account, Base, Character, StateChangeLog


@pytest_asyncio.fixture()
async def db():
    """In-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
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
# Scene constraints: what the LLM is entitled to change in this scene
# ---------------------------------------------------------------------------

_REGISTRY = {"merchant_guild": {}, "knights": {}}


def _constrain(mutations, **kw):
    defaults = {
        "npc_id": "halvric",
        "npc_faction_id": "merchant_guild",
        "faction_registry": _REGISTRY,
        "allowed_flags": {"heard_the_bell"},
    }
    return constrain_world_mutations(mutations, **{**defaults, **kw})


def _rel(delta, npc_id=""):
    return {"type": "relationship_change", "npc_id": npc_id, "delta": delta, "reason": "r"}


def _fac(faction_id, delta):
    return {"type": "faction_standing_change", "faction_id": faction_id, "delta": delta, "reason": "r"}


def _flag(flag):
    return {"type": "world_flag_set", "flag": flag, "reason": "r"}


class TestConstrainWorldMutations:
    def test_relationship_always_targets_the_npc_in_the_scene(self):
        assert _constrain([_rel(3, "some_other_npc")]) == [_rel(3, "halvric")]

    def test_unknown_faction_dropped(self):
        assert _constrain([_fac("made_up_faction", -10)]) == []

    def test_no_registry_drops_every_faction_change(self):
        assert _constrain([_fac("merchant_guild", -10)], faction_registry=None) == []

    def test_gain_only_with_the_npcs_own_faction(self):
        assert _constrain([_fac("merchant_guild", 5)]) == [_fac("merchant_guild", 5)]
        assert _constrain([_fac("knights", 5)]) == []
        assert _constrain([_fac("merchant_guild", 5)], npc_faction_id=None) == []

    def test_loss_allowed_with_any_real_faction(self):
        assert _constrain([_fac("knights", -20)]) == [_fac("knights", -20)]

    def test_invented_flag_dropped(self):
        assert _constrain([_flag("any_quest_flag_i_like")]) == []

    def test_listed_story_flag_kept(self):
        assert _constrain([_flag("heard_the_bell")]) == [_flag("heard_the_bell")]

    def test_crime_flags_kept(self):
        kept = _constrain([_flag("wanted_in:merchant_quarter"), _flag("bounty_merchant_guild")])
        assert [m["flag"] for m in kept] == ["wanted_in:merchant_quarter", "bounty_merchant_guild"]

    def test_one_change_per_target_per_turn(self):
        assert _constrain([_rel(5), _rel(5), _rel(5)]) == [_rel(5, "halvric")]


class TestSessionGainCaps:
    async def _apply(self, db, mutations, session_id="sess_a"):
        capped = await apply_session_gain_caps(db, "char_001", session_id, mutations)
        relationships: dict[str, int] = {}
        standing: dict[str, int] = {}
        await apply_world_mutations(
            db,
            character_id="char_001",
            mutations=capped,
            faction_standing=standing,
            relationships=relationships,
            faction_registry=_REGISTRY,
            session_id=session_id,
        )
        await db.flush()
        return capped

    @pytest.mark.asyncio
    async def test_relationship_gains_stop_at_the_session_cap(self, db):
        total = 0
        for _ in range(10):
            capped = await self._apply(db, [_rel(MAX_POSITIVE_DELTA_PER_TURN, "halvric")])
            total += sum(m["delta"] for m in capped)
        assert total == MAX_POSITIVE_RELATIONSHIP_PER_SESSION

    @pytest.mark.asyncio
    async def test_faction_gains_stop_at_the_session_cap(self, db):
        total = 0
        for _ in range(10):
            capped = await self._apply(db, [_fac("merchant_guild", MAX_POSITIVE_DELTA_PER_TURN)])
            total += sum(m["delta"] for m in capped)
        assert total == MAX_POSITIVE_FACTION_PER_SESSION

    @pytest.mark.asyncio
    async def test_cap_is_per_session_and_per_npc(self, db):
        for _ in range(10):
            await self._apply(db, [_rel(5, "halvric")])
        other_npc = await self._apply(db, [_rel(5, "tinte")])
        new_session = await self._apply(db, [_rel(5, "halvric")], session_id="sess_b")
        assert other_npc == [_rel(5, "tinte")]
        assert new_session == [_rel(5, "halvric")]

    @pytest.mark.asyncio
    async def test_losses_and_flags_are_never_capped(self, db):
        for _ in range(10):
            await self._apply(db, [_rel(5, "halvric")])
        mutations = [_rel(-30, "halvric"), _flag("wanted_in:merchant_quarter")]
        assert await self._apply(db, mutations) == mutations


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateWorldMutations:
    def test_empty_list(self):
        assert validate_world_mutations([]) == []

    def test_valid_faction_change(self):
        result = validate_world_mutations(
            [{"type": "faction_standing_change", "faction_id": "merchants_guild", "delta": -15, "reason": "theft"}]
        )
        assert len(result) == 1
        assert result[0]["type"] == "faction_standing_change"
        assert result[0]["faction_id"] == "merchants_guild"
        assert result[0]["delta"] == -15
        assert result[0]["reason"] == "theft"

    def test_valid_relationship_change(self):
        result = validate_world_mutations(
            [{"type": "relationship_change", "npc_id": "shopkeeper_mara", "delta": -30, "reason": "robbed"}]
        )
        assert len(result) == 1
        assert result[0]["type"] == "relationship_change"
        assert result[0]["npc_id"] == "shopkeeper_mara"
        assert result[0]["delta"] == -30

    def test_valid_world_flag_set(self):
        result = validate_world_mutations(
            [{"type": "world_flag_set", "flag": "wanted_in:market_district", "reason": "witnessed crime"}]
        )
        assert len(result) == 1
        assert result[0]["type"] == "world_flag_set"
        assert result[0]["flag"] == "wanted_in:market_district"

    def test_invalid_type_skipped(self):
        result = validate_world_mutations([{"type": "invalid_type", "reason": "test"}])
        assert result == []

    def test_missing_reason_skipped(self):
        result = validate_world_mutations([{"type": "world_flag_set", "flag": "test_flag", "reason": ""}])
        assert result == []

    def test_missing_faction_id_skipped(self):
        result = validate_world_mutations([{"type": "faction_standing_change", "delta": -10, "reason": "test"}])
        assert result == []

    def test_relationship_change_needs_no_npc_id(self):
        """The LLM is not told NPC ids; the scene assigns the target later."""
        result = validate_world_mutations([{"type": "relationship_change", "delta": -10, "reason": "test"}])
        assert result == [{"type": "relationship_change", "npc_id": "", "delta": -10, "reason": "test"}]

    def test_missing_flag_skipped(self):
        result = validate_world_mutations([{"type": "world_flag_set", "reason": "test"}])
        assert result == []

    def test_delta_clamped_to_max(self):
        result = validate_world_mutations(
            [{"type": "faction_standing_change", "faction_id": "guild", "delta": 200, "reason": "test"}]
        )
        assert result[0]["delta"] == MAX_POSITIVE_DELTA_PER_TURN

    def test_delta_clamped_to_negative_max(self):
        result = validate_world_mutations(
            [{"type": "faction_standing_change", "faction_id": "guild", "delta": -200, "reason": "test"}]
        )
        assert result[0]["delta"] == -MAX_NEGATIVE_DELTA

    def test_gains_are_capped_far_below_losses(self):
        """Trust is slow to earn: one turn cannot move a tier."""
        assert MAX_POSITIVE_DELTA_PER_TURN < 11  # below the first positive tier threshold
        assert MAX_POSITIVE_DELTA_PER_TURN < MAX_NEGATIVE_DELTA

    def test_non_numeric_delta_dropped(self):
        result = validate_world_mutations(
            [{"type": "faction_standing_change", "faction_id": "guild", "delta": "lots", "reason": "test"}]
        )
        assert result == []

    def test_zero_delta_dropped(self):
        result = validate_world_mutations(
            [{"type": "faction_standing_change", "faction_id": "guild", "delta": 0, "reason": "test"}]
        )
        assert result == []

    def test_none_delta_treated_as_zero(self):
        result = validate_world_mutations([{"type": "relationship_change", "npc_id": "npc_a", "reason": "test"}])
        assert result == []

    def test_cap_at_max_mutations(self):
        mutations = [
            {"type": "world_flag_set", "flag": f"flag_{i}", "reason": "test"}
            for i in range(_MAX_MUTATIONS_PER_TURN + 5)
        ]
        result = validate_world_mutations(mutations)
        assert len(result) == _MAX_MUTATIONS_PER_TURN

    def test_mixed_valid_and_invalid(self):
        mutations = [
            {"type": "world_flag_set", "flag": "valid_flag", "reason": "ok"},
            {"type": "bogus", "reason": "nope"},
            {"type": "faction_standing_change", "faction_id": "guild", "delta": 10, "reason": "reward"},
        ]
        result = validate_world_mutations(mutations)
        assert len(result) == 2
        assert result[0]["type"] == "world_flag_set"
        assert result[1]["type"] == "faction_standing_change"


# ---------------------------------------------------------------------------
# Application tests
# ---------------------------------------------------------------------------


class TestApplyWorldMutations:
    @pytest.mark.asyncio
    async def test_apply_world_flag(self, db):
        mutations = [{"type": "world_flag_set", "flag": "bounty_active", "reason": "crime"}]
        result = await apply_world_mutations(db, "char_001", mutations, session_id="sess_01")
        await db.flush()

        assert len(result["flags_set"]) == 1
        assert result["flags_set"][0]["flag"] == "bounty_active"

        # Check that StateChangeLog was created
        logs = (await db.execute(select(StateChangeLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].change_type == "world_flag_set"
        assert logs[0].flag == "bounty_active"
        assert logs[0].character_id == "char_001"

    @pytest.mark.asyncio
    async def test_apply_faction_change_without_standing_skipped(self, db):
        """Faction mutations are skipped when faction_standing is None."""
        mutations = [{"type": "faction_standing_change", "faction_id": "guild", "delta": -10, "reason": "theft"}]
        result = await apply_world_mutations(db, "char_001", mutations)
        assert result["faction_changes"] == []

    @pytest.mark.asyncio
    async def test_apply_faction_change_with_standing(self, db):
        standing = {"merchants_guild": 50}
        mutations = [
            {
                "type": "faction_standing_change",
                "faction_id": "merchants_guild",
                "delta": -15,
                "reason": "theft_witnessed",
            }
        ]
        result = await apply_world_mutations(db, "char_001", mutations, faction_standing=standing, session_id="sess_01")
        await db.flush()

        assert len(result["faction_changes"]) == 1
        change = result["faction_changes"][0]
        assert change["faction_id"] == "merchants_guild"
        assert change["old"] == 50
        assert change["new"] == 35
        assert change["delta"] == -15

        # Standing dict was mutated in place
        assert standing["merchants_guild"] == 35

        # Log was created
        logs = (await db.execute(select(StateChangeLog))).scalars().all()
        assert any(log.change_type == "faction_standing_change" for log in logs)

    @pytest.mark.asyncio
    async def test_apply_relationship_change_without_relationships_skipped(self, db):
        mutations = [{"type": "relationship_change", "npc_id": "npc_a", "delta": -20, "reason": "attacked"}]
        result = await apply_world_mutations(db, "char_001", mutations)
        assert result["relationship_changes"] == []

    @pytest.mark.asyncio
    async def test_apply_relationship_change_with_relationships(self, db):
        relationships = {"shopkeeper_mara": 30}
        mutations = [
            {
                "type": "relationship_change",
                "npc_id": "shopkeeper_mara",
                "delta": -30,
                "reason": "robbed",
            }
        ]
        result = await apply_world_mutations(
            db, "char_001", mutations, relationships=relationships, session_id="sess_01"
        )
        await db.flush()

        assert len(result["relationship_changes"]) == 1
        assert relationships["shopkeeper_mara"] == 0

        logs = (await db.execute(select(StateChangeLog))).scalars().all()
        assert any(log.change_type == "relationship_change" for log in logs)

    @pytest.mark.asyncio
    async def test_apply_multiple_mutations(self, db):
        standing = {"city_guard": 20}
        relationships = {"guard_captain": 10}
        mutations = [
            {"type": "faction_standing_change", "faction_id": "city_guard", "delta": -25, "reason": "assault"},
            {"type": "relationship_change", "npc_id": "guard_captain", "delta": -40, "reason": "attacked guard"},
            {"type": "world_flag_set", "flag": "wanted_in:market_district", "reason": "witnessed assault"},
        ]
        result = await apply_world_mutations(
            db,
            "char_001",
            mutations,
            faction_standing=standing,
            relationships=relationships,
            session_id="sess_01",
        )
        await db.flush()

        assert len(result["faction_changes"]) == 1
        assert len(result["relationship_changes"]) == 1
        assert len(result["flags_set"]) == 1

        # Verify all three logs
        logs = (await db.execute(select(StateChangeLog))).scalars().all()
        log_types = {log.change_type for log in logs}
        assert log_types == {"faction_standing_change", "relationship_change", "world_flag_set"}

    @pytest.mark.asyncio
    async def test_empty_mutations_returns_empty_report(self, db):
        result = await apply_world_mutations(db, "char_001", [])
        assert result == {"faction_changes": [], "relationship_changes": [], "flags_set": []}


# ---------------------------------------------------------------------------
# Mutation model tests
# ---------------------------------------------------------------------------


class TestMutationModels:
    def test_faction_standing_mutation_valid(self):
        from relay.mutations import FactionStandingMutation

        m = FactionStandingMutation(
            character_id="char_001",
            faction_id="guild",
            delta=-15,
            old_value=50,
            new_value=35,
            reason="theft",
        )
        assert m.source == "consequence"

    def test_relationship_mutation_valid(self):
        from relay.mutations import RelationshipMutation

        m = RelationshipMutation(
            character_id="char_001",
            npc_id="shopkeeper",
            delta=-30,
            old_value=30,
            new_value=0,
            reason="robbed",
        )
        assert m.source == "consequence"

    def test_world_flag_mutation_valid(self):
        from relay.mutations import WorldFlagMutation

        m = WorldFlagMutation(
            character_id="char_001",
            flag="wanted_in:market_district",
            reason="witnessed crime",
        )
        assert m.value == "true"
        assert m.source == "consequence"

    def test_faction_mutation_forbids_extra_fields(self):
        from pydantic import ValidationError

        from relay.mutations import FactionStandingMutation

        with pytest.raises(ValidationError):
            FactionStandingMutation(
                character_id="char_001",
                faction_id="guild",
                delta=-15,
                old_value=50,
                new_value=35,
                reason="theft",
                extra_field="bad",
            )


# ---------------------------------------------------------------------------
# Schema integration test
# ---------------------------------------------------------------------------


class TestAnalysisToolWorldMutations:
    def test_schema_includes_world_mutations(self):
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        schema = _ANALYSIS_TOOL["input_schema"]
        assert "world_mutations" in schema["properties"]
        wm = schema["properties"]["world_mutations"]
        assert wm["type"] == "array"
        # Check the enum on type field
        item_props = wm["items"]["properties"]
        assert "faction_standing_change" in item_props["type"]["enum"]
        assert "relationship_change" in item_props["type"]["enum"]
        assert "world_flag_set" in item_props["type"]["enum"]
        # Delta is clamped
        assert item_props["delta"]["minimum"] == -MAX_NEGATIVE_DELTA
        assert item_props["delta"]["maximum"] == MAX_POSITIVE_DELTA_PER_TURN

    def test_world_mutations_not_required(self):
        """world_mutations is optional — existing analysis results without it still pass."""
        from relay.endpoints.dialogue import _ANALYSIS_TOOL

        required = _ANALYSIS_TOOL["input_schema"]["required"]
        assert "world_mutations" not in required
