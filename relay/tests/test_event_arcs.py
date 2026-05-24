"""Tests for event arc blueprint system — §4 Blueprint Pattern.

Covers blueprint loading, phase selection, rule enforcement,
roster seeding, and full arc instantiation.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from relay.generation.arc_instantiator import (
    _satisfies_excluded_combinations,
    _satisfies_final_phase,
    _satisfies_max_consecutive,
    _satisfies_must_include,
    instantiate_arc,
    seed_roster,
    select_phases,
)
from relay.schemas import (
    EventArcBlueprint,
    EventArcInstance,
    PhaseInstance,
    PhaseTemplate,
    SelectionRules,
)

# ---------------------------------------------------------------------------
# Shared fixture: a minimal valid blueprint
# ---------------------------------------------------------------------------


def _make_phase(phase_id: str, category: str = "combat", **overrides) -> PhaseTemplate:
    base = {
        "phase_id": phase_id,
        "name": phase_id.replace("_", " ").title(),
        "category": category,
        "description": f"Test phase: {phase_id}",
        "checks": [{"type": "strength", "dc": 12}],
    }
    base.update(overrides)
    return PhaseTemplate.model_validate(base)


def _make_blueprint(**overrides) -> EventArcBlueprint:
    base = {
        "id": "test_arc",
        "world_id": "inkglass_dark",
        "name": "Test Arc",
        "description": "A test event arc.",
        "region_id": "test_region",
        "level_range": [3, 8],
        "phase_count_range": [3, 5],
        "phase_pool": [
            {
                "phase_id": "combat_1",
                "name": "Combat One",
                "category": "combat",
                "description": "Fight!",
                "checks": [{"type": "attack_roll", "dc": 14}],
                "terrain_pool": ["arena"],
                "examiner_pool": ["examiner_a"],
            },
            {
                "phase_id": "combat_2",
                "name": "Combat Two",
                "category": "combat",
                "description": "Fight again!",
                "checks": [{"type": "attack_roll", "dc": 12}],
            },
            {
                "phase_id": "social_1",
                "name": "Social One",
                "category": "social",
                "description": "Talk!",
                "checks": [{"type": "persuasion", "dc": 13}],
                "examiner_pool": ["examiner_b"],
            },
            {
                "phase_id": "puzzle_1",
                "name": "Puzzle One",
                "category": "puzzle",
                "description": "Think!",
                "checks": [{"type": "intelligence", "dc": 14}],
            },
            {
                "phase_id": "stealth_1",
                "name": "Stealth One",
                "category": "stealth",
                "description": "Sneak!",
                "checks": [{"type": "stealth", "dc": 14}],
                "hazard_pool": ["guard_patrol", "alarm_ward"],
            },
            {
                "phase_id": "endurance_1",
                "name": "Endurance One",
                "category": "endurance",
                "description": "Endure!",
                "checks": [{"type": "constitution", "dc": 13}],
            },
        ],
        "selection_rules": {
            "must_include_categories": ["combat"],
            "final_phase_categories": ["combat", "social"],
            "max_consecutive_same_category": 2,
        },
        "npc_roster": {
            "anchor_npcs": ["npc_anchor_1", "npc_anchor_2"],
            "generated_count_range": [2, 4],
            "generated_template_roles": ["guard", "traveler"],
        },
        "completion_rewards": {
            "faction_change": {"faction_id": "test_guild", "amount": 10},
            "items": ["test_badge"],
        },
    }
    base.update(overrides)
    return EventArcBlueprint.model_validate(base)


# ---------------------------------------------------------------------------
# Pydantic model tests
# ---------------------------------------------------------------------------


class TestPhaseTemplate:
    def test_valid_phase(self):
        p = _make_phase("test_phase", "combat")
        assert p.phase_id == "test_phase"
        assert p.category == "combat"

    def test_invalid_category_rejected(self):
        with pytest.raises(ValidationError):
            _make_phase("test_phase", "flying")

    def test_checks_required(self):
        with pytest.raises(ValidationError):
            PhaseTemplate.model_validate(
                {
                    "phase_id": "no_checks",
                    "name": "No Checks",
                    "category": "combat",
                    "description": "Missing checks",
                    "checks": [],
                }
            )


class TestSelectionRules:
    def test_defaults(self):
        rules = SelectionRules()
        assert rules.must_include_categories == []
        assert rules.max_consecutive_same_category == 2

    def test_max_consecutive_bounds(self):
        rules = SelectionRules(max_consecutive_same_category=5)
        assert rules.max_consecutive_same_category == 5

        with pytest.raises(ValidationError):
            SelectionRules(max_consecutive_same_category=0)


class TestEventArcBlueprint:
    def test_valid_blueprint(self):
        bp = _make_blueprint()
        assert bp.id == "test_arc"
        assert len(bp.phase_pool) == 6

    def test_minimum_phase_pool_size(self):
        with pytest.raises(ValidationError):
            _make_blueprint(
                phase_pool=[
                    {
                        "phase_id": "a",
                        "name": "A",
                        "category": "combat",
                        "description": "X",
                        "checks": [{"type": "str", "dc": 10}],
                    },
                    {
                        "phase_id": "b",
                        "name": "B",
                        "category": "social",
                        "description": "X",
                        "checks": [{"type": "str", "dc": 10}],
                    },
                ]
            )

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            EventArcBlueprint.model_validate({"id": "broken"})


class TestEventArcInstance:
    def test_valid_instance(self):
        inst = EventArcInstance(
            id="test_inst",
            blueprint_id="test_arc",
            world_id="inkglass_dark",
            character_id="player_1",
            origin="authored",
            status="active",
            current_phase_index=0,
            phases=[PhaseInstance(phase_id="combat_1", status="active")],
        )
        assert inst.id == "test_inst"
        assert inst.origin == "authored"

    def test_invalid_origin_rejected(self):
        with pytest.raises(ValidationError):
            EventArcInstance(
                id="test",
                blueprint_id="bp",
                world_id="w",
                character_id="c",
                origin="hacked",
                status="active",
                current_phase_index=0,
                phases=[PhaseInstance(phase_id="p", status="active")],
            )

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            EventArcInstance(
                id="test",
                blueprint_id="bp",
                world_id="w",
                character_id="c",
                origin="authored",
                status="winning",
                current_phase_index=0,
                phases=[PhaseInstance(phase_id="p", status="active")],
            )


# ---------------------------------------------------------------------------
# Selection rule helper tests
# ---------------------------------------------------------------------------


class TestSatisfiesMustInclude:
    def test_satisfied(self):
        phases = [_make_phase("a", "combat"), _make_phase("b", "social")]
        assert _satisfies_must_include(phases, ["combat", "social"]) is True

    def test_not_satisfied(self):
        phases = [_make_phase("a", "combat"), _make_phase("b", "combat")]
        assert _satisfies_must_include(phases, ["combat", "social"]) is False

    def test_empty_requirement(self):
        phases = [_make_phase("a", "combat")]
        assert _satisfies_must_include(phases, []) is True


class TestSatisfiesFinalPhase:
    def test_satisfied(self):
        phases = [_make_phase("a", "social"), _make_phase("b", "combat")]
        assert _satisfies_final_phase(phases, ["combat"]) is True

    def test_not_satisfied(self):
        phases = [_make_phase("a", "combat"), _make_phase("b", "social")]
        assert _satisfies_final_phase(phases, ["combat"]) is False

    def test_empty_constraint(self):
        phases = [_make_phase("a", "puzzle")]
        assert _satisfies_final_phase(phases, []) is True


class TestSatisfiesMaxConsecutive:
    def test_satisfied(self):
        phases = [
            _make_phase("a", "combat"),
            _make_phase("b", "combat"),
            _make_phase("c", "social"),
        ]
        assert _satisfies_max_consecutive(phases, 2) is True

    def test_not_satisfied(self):
        phases = [
            _make_phase("a", "combat"),
            _make_phase("b", "combat"),
            _make_phase("c", "combat"),
        ]
        assert _satisfies_max_consecutive(phases, 2) is False

    def test_single_phase(self):
        phases = [_make_phase("a", "combat")]
        assert _satisfies_max_consecutive(phases, 1) is True


class TestSatisfiesExcludedCombinations:
    def test_satisfied(self):
        phases = [_make_phase("a"), _make_phase("b")]
        assert _satisfies_excluded_combinations(phases, [["a", "c"]]) is True

    def test_not_satisfied(self):
        phases = [_make_phase("a"), _make_phase("b")]
        assert _satisfies_excluded_combinations(phases, [["a", "b"]]) is False

    def test_empty_exclusions(self):
        phases = [_make_phase("a"), _make_phase("b")]
        assert _satisfies_excluded_combinations(phases, []) is True


# ---------------------------------------------------------------------------
# Phase selection tests
# ---------------------------------------------------------------------------


class TestSelectPhases:
    def test_selects_correct_count(self):
        bp = _make_blueprint(phase_count_range=[3, 3])
        phases = select_phases(bp, rng=random.Random(42))
        assert len(phases) == 3

    def test_selects_within_count_range(self):
        bp = _make_blueprint(phase_count_range=[3, 5])
        rng = random.Random(42)
        for _ in range(20):
            phases = select_phases(bp, rng=rng)
            assert 3 <= len(phases) <= 5

    def test_includes_required_category(self):
        bp = _make_blueprint(
            selection_rules={
                "must_include_categories": ["combat"],
                "max_consecutive_same_category": 10,
            }
        )
        rng = random.Random(42)
        for _ in range(20):
            phases = select_phases(bp, rng=rng)
            categories = {p.category for p in phases}
            assert "combat" in categories

    def test_final_phase_constraint(self):
        bp = _make_blueprint(
            selection_rules={
                "final_phase_categories": ["combat"],
                "max_consecutive_same_category": 10,
            }
        )
        rng = random.Random(42)
        for _ in range(20):
            phases = select_phases(bp, rng=rng)
            assert phases[-1].category == "combat"

    def test_max_consecutive_enforced(self):
        bp = _make_blueprint(
            selection_rules={
                "max_consecutive_same_category": 1,
            }
        )
        rng = random.Random(42)
        for _ in range(20):
            phases = select_phases(bp, rng=rng)
            for i in range(1, len(phases)):
                assert phases[i].category != phases[i - 1].category

    def test_excluded_combinations_enforced(self):
        bp = _make_blueprint(
            selection_rules={
                "excluded_combinations": [["combat_1", "combat_2"]],
                "max_consecutive_same_category": 10,
            }
        )
        rng = random.Random(42)
        for _ in range(20):
            phases = select_phases(bp, rng=rng)
            ids = {p.phase_id for p in phases}
            assert not ("combat_1" in ids and "combat_2" in ids), f"Excluded pair both present: {ids}"

    def test_deterministic_with_seed(self):
        bp = _make_blueprint()
        phases_a = select_phases(bp, rng=random.Random(42))
        phases_b = select_phases(bp, rng=random.Random(42))
        assert [p.phase_id for p in phases_a] == [p.phase_id for p in phases_b]

    def test_phase_count_capped_by_pool_size(self):
        bp = _make_blueprint(phase_count_range=[10, 10])
        phases = select_phases(bp, rng=random.Random(42))
        assert len(phases) <= len(bp.phase_pool)


# ---------------------------------------------------------------------------
# Roster seeding tests
# ---------------------------------------------------------------------------


class TestSeedRoster:
    def test_anchor_npcs_present(self):
        bp = _make_blueprint()
        roster = seed_roster(bp, rng=random.Random(42))
        anchor_ids = {c.npc_id for c in roster if c.is_anchor}
        assert "npc_anchor_1" in anchor_ids
        assert "npc_anchor_2" in anchor_ids

    def test_anchors_marked_correctly(self):
        bp = _make_blueprint()
        roster = seed_roster(bp, rng=random.Random(42))
        for c in roster:
            if c.npc_id.startswith("npc_anchor"):
                assert c.is_anchor is True
            else:
                assert c.is_anchor is False

    def test_generated_count_in_range(self):
        bp = _make_blueprint(
            npc_roster={
                "anchor_npcs": ["anchor_1"],
                "generated_count_range": [3, 5],
                "generated_template_roles": ["guard"],
            }
        )
        rng = random.Random(42)
        for _ in range(20):
            roster = seed_roster(bp, rng=rng)
            gen_count = sum(1 for c in roster if not c.is_anchor)
            assert 3 <= gen_count <= 5

    def test_all_start_active(self):
        bp = _make_blueprint()
        roster = seed_roster(bp, rng=random.Random(42))
        for c in roster:
            assert c.status == "active"

    def test_initial_relationship_zero(self):
        bp = _make_blueprint()
        roster = seed_roster(bp, rng=random.Random(42))
        for c in roster:
            assert c.relationship_score == 0

    def test_generated_ids_contain_role(self):
        bp = _make_blueprint(
            npc_roster={
                "anchor_npcs": [],
                "generated_count_range": [2, 2],
                "generated_template_roles": ["guard"],
            }
        )
        roster = seed_roster(bp, rng=random.Random(42))
        for c in roster:
            assert "gen_guard_" in c.npc_id

    def test_no_generated_when_range_zero(self):
        bp = _make_blueprint(
            npc_roster={
                "anchor_npcs": ["anchor_1"],
                "generated_count_range": [0, 0],
                "generated_template_roles": [],
            }
        )
        roster = seed_roster(bp, rng=random.Random(42))
        assert len(roster) == 1
        assert roster[0].npc_id == "anchor_1"


# ---------------------------------------------------------------------------
# Full instantiation tests
# ---------------------------------------------------------------------------


class TestInstantiateArc:
    def test_creates_valid_instance(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert isinstance(inst, EventArcInstance)

    def test_instance_has_correct_blueprint_id(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.blueprint_id == "test_arc"

    def test_instance_has_correct_world_id(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.world_id == "inkglass_dark"

    def test_instance_has_correct_character_id(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.character_id == "player_1"

    def test_instance_origin_is_authored(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.origin == "authored"

    def test_instance_status_is_active(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.status == "active"

    def test_instance_starts_at_phase_zero(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.current_phase_index == 0

    def test_first_phase_is_active_rest_pending(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.phases[0].status == "active"
        for phase in inst.phases[1:]:
            assert phase.status == "pending"

    def test_phases_from_pool(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        pool_ids = {p.phase_id for p in bp.phase_pool}
        for phase in inst.phases:
            assert phase.phase_id in pool_ids

    def test_examiners_assigned_from_pool(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        # At least one phase should have an examiner (combat_1 and social_1 have examiner_pools)
        examiners = [p.examiner_npc_id for p in inst.phases if p.examiner_npc_id]
        assert len(examiners) >= 1

    def test_terrain_assigned_from_pool(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        # combat_1 has terrain_pool ["arena"]
        for phase in inst.phases:
            if phase.phase_id == "combat_1":
                assert phase.terrain == "arena"

    def test_candidates_include_anchors(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        anchor_ids = {c.npc_id for c in inst.candidates if c.is_anchor}
        assert "npc_anchor_1" in anchor_ids
        assert "npc_anchor_2" in anchor_ids

    def test_explicit_instance_id(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42), instance_id="custom_id")
        assert inst.id == "custom_id"

    def test_auto_generated_id_format(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert inst.id.startswith("test_arc_player_1_")

    def test_session_id_propagated(self):
        bp = _make_blueprint()
        inst = instantiate_arc(bp, "player_1", rng=random.Random(42), session_id="session_123")
        assert inst.session_id == "session_123"

    def test_deterministic_with_seed(self):
        bp = _make_blueprint()
        inst_a = instantiate_arc(bp, "player_1", rng=random.Random(42))
        inst_b = instantiate_arc(bp, "player_1", rng=random.Random(42))
        assert [p.phase_id for p in inst_a.phases] == [p.phase_id for p in inst_b.phases]

    def test_phase_count_respects_range(self):
        bp = _make_blueprint(phase_count_range=[3, 5])
        rng = random.Random(42)
        for _ in range(20):
            inst = instantiate_arc(bp, "player_1", rng=rng)
            assert 3 <= len(inst.phases) <= 5


# ---------------------------------------------------------------------------
# Blueprint loading tests
# ---------------------------------------------------------------------------


class TestBlueprintLoading:
    @pytest.mark.asyncio
    async def test_load_inkglass_dark_blueprints(self):
        from relay.generation.blueprint_loader import clear_cache, load_blueprints

        clear_cache()
        blueprints = await load_blueprints("inkglass_dark")
        assert len(blueprints) >= 1
        assert "guild_trials" in blueprints

    @pytest.mark.asyncio
    async def test_load_nonexistent_world(self):
        from relay.generation.blueprint_loader import clear_cache, load_blueprints

        clear_cache()
        blueprints = await load_blueprints("nonexistent_world")
        assert blueprints == {}

    @pytest.mark.asyncio
    async def test_get_blueprint(self):
        from relay.generation.blueprint_loader import clear_cache, get_blueprint

        clear_cache()
        bp = await get_blueprint("inkglass_dark", "guild_trials")
        assert bp is not None
        assert bp.id == "guild_trials"
        assert bp.name == "The Guild Trials"

    @pytest.mark.asyncio
    async def test_get_missing_blueprint(self):
        from relay.generation.blueprint_loader import clear_cache, get_blueprint

        clear_cache()
        bp = await get_blueprint("inkglass_dark", "nonexistent_arc")
        assert bp is None


# ---------------------------------------------------------------------------
# Integration test: load blueprint → instantiate
# ---------------------------------------------------------------------------


class TestBlueprintToInstance:
    @pytest.mark.asyncio
    async def test_load_and_instantiate(self):
        from relay.generation.blueprint_loader import clear_cache, get_blueprint

        clear_cache()
        bp = await get_blueprint("inkglass_dark", "guild_trials")
        assert bp is not None

        inst = instantiate_arc(bp, "test_player", rng=random.Random(42))
        assert isinstance(inst, EventArcInstance)
        assert inst.blueprint_id == "guild_trials"
        assert inst.world_id == "inkglass_dark"
        assert 3 <= len(inst.phases) <= 5
        # Must include combat (selection rule)
        phase_ids = {p.phase_id for p in inst.phases}
        bp_combat_ids = {p.phase_id for p in bp.phase_pool if p.category == "combat"}
        assert len(phase_ids & bp_combat_ids) >= 1
        # Final phase should be combat or social
        final_phase_id = inst.phases[-1].phase_id
        final_category = next(p.category for p in bp.phase_pool if p.phase_id == final_phase_id)
        assert final_category in ["combat", "social"]
        # Excluded combination enforced
        assert not ("arena_combat" in phase_ids and "beast_hunt" in phase_ids)
