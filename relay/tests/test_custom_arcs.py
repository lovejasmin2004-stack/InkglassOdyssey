"""Tests for custom arc remixing — §5 Custom Arc Remixing.

Covers validation, surprise-me filling, rule enforcement,
featured NPC integration, and the custom arc request model.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from relay.generation.custom_arc_assembler import (
    SURPRISE_ME,
    CustomArcError,
    assemble_custom_arc,
    validate_custom_arc_request,
)
from relay.schemas import (
    CustomArcRequest,
    EventArcBlueprint,
    EventArcInstance,
)

# ---------------------------------------------------------------------------
# Shared fixture: reuse the blueprint from test_event_arcs
# ---------------------------------------------------------------------------


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
            "anchor_npcs": ["npc_anchor_1"],
            "generated_count_range": [1, 2],
            "generated_template_roles": ["guard"],
        },
        "completion_rewards": {
            "faction_change": {"faction_id": "test_guild", "amount": 10},
            "items": ["test_badge"],
        },
    }
    base.update(overrides)
    return EventArcBlueprint.model_validate(base)


# ---------------------------------------------------------------------------
# CustomArcRequest model tests
# ---------------------------------------------------------------------------


class TestCustomArcRequest:
    def test_valid_request(self):
        req = CustomArcRequest(
            blueprint_id="guild_trials",
            world_id="inkglass_dark",
            phase_choices=["combat_1", "surprise_me", "social_1"],
        )
        assert req.blueprint_id == "guild_trials"
        assert len(req.phase_choices) == 3

    def test_empty_phase_choices_rejected(self):
        with pytest.raises(ValidationError):
            CustomArcRequest(
                blueprint_id="guild_trials",
                world_id="inkglass_dark",
                phase_choices=[],
            )

    def test_too_many_phase_choices_rejected(self):
        with pytest.raises(ValidationError):
            CustomArcRequest(
                blueprint_id="guild_trials",
                world_id="inkglass_dark",
                phase_choices=["a"] * 11,
            )

    def test_featured_npcs_default_empty(self):
        req = CustomArcRequest(
            blueprint_id="guild_trials",
            world_id="inkglass_dark",
            phase_choices=["surprise_me", "surprise_me", "surprise_me"],
        )
        assert req.featured_npc_ids == []

    def test_region_override(self):
        req = CustomArcRequest(
            blueprint_id="guild_trials",
            world_id="inkglass_dark",
            phase_choices=["surprise_me"] * 3,
            region_id="custom_region",
        )
        assert req.region_id == "custom_region"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateCustomArcRequest:
    def test_valid_all_explicit(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, ["combat_1", "social_1", "puzzle_1"])
        assert errors == []

    def test_valid_all_surprise_me(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME])
        assert errors == []

    def test_valid_mixed(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, ["combat_1", SURPRISE_ME, SURPRISE_ME])
        assert errors == []

    def test_too_few_phases(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, ["combat_1"])
        assert any("out of range" in e for e in errors)

    def test_too_many_phases(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(
            bp, ["combat_1", "combat_2", "social_1", "puzzle_1", "stealth_1", "endurance_1"]
        )
        assert any("out of range" in e for e in errors)

    def test_unknown_phase_id(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, ["combat_1", "nonexistent_phase", "social_1"])
        assert any("not in blueprint" in e for e in errors)

    def test_duplicate_phases(self):
        bp = _make_blueprint()
        errors = validate_custom_arc_request(bp, ["combat_1", "combat_1", "social_1"])
        assert any("Duplicate" in e for e in errors)


# ---------------------------------------------------------------------------
# Assembly tests
# ---------------------------------------------------------------------------


class TestAssembleCustomArc:
    def test_all_explicit_phases(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            ["combat_1", "social_1", "puzzle_1"],
            rng=random.Random(42),
        )
        assert isinstance(inst, EventArcInstance)
        assert inst.origin == "custom"
        phase_ids = [p.phase_id for p in inst.phases]
        assert "combat_1" in phase_ids
        assert "social_1" in phase_ids
        assert "puzzle_1" in phase_ids

    def test_all_surprise_me(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert isinstance(inst, EventArcInstance)
        assert inst.origin == "custom"
        assert len(inst.phases) == 3

    def test_mixed_explicit_and_surprise(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            ["combat_1", SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert inst.phases[0].phase_id == "combat_1" or any(p.phase_id == "combat_1" for p in inst.phases)
        assert len(inst.phases) == 3

    def test_origin_is_custom(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert inst.origin == "custom"

    def test_status_is_active(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert inst.status == "active"

    def test_first_phase_active_rest_pending(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert inst.phases[0].status == "active"
        for phase in inst.phases[1:]:
            assert phase.status == "pending"

    def test_explicit_instance_id(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
            instance_id="custom_123",
        )
        assert inst.id == "custom_123"

    def test_auto_generated_id_contains_custom(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert "custom" in inst.id

    def test_session_id_propagated(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
            session_id="sess_1",
        )
        assert inst.session_id == "sess_1"

    def test_deterministic_with_seed(self):
        bp = _make_blueprint()
        inst_a = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        inst_b = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        assert [p.phase_id for p in inst_a.phases] == [p.phase_id for p in inst_b.phases]

    def test_blueprint_anchors_included(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        anchor_ids = {c.npc_id for c in inst.candidates if c.is_anchor}
        assert "npc_anchor_1" in anchor_ids


# ---------------------------------------------------------------------------
# Featured NPC tests
# ---------------------------------------------------------------------------


class TestFeaturedNpcs:
    def test_featured_npcs_added(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            featured_npc_ids=["rival_npc", "ally_npc"],
            rng=random.Random(42),
        )
        candidate_ids = {c.npc_id for c in inst.candidates}
        assert "rival_npc" in candidate_ids
        assert "ally_npc" in candidate_ids

    def test_featured_npcs_not_anchors(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            featured_npc_ids=["rival_npc"],
            rng=random.Random(42),
        )
        rival = next(c for c in inst.candidates if c.npc_id == "rival_npc")
        assert rival.is_anchor is False

    def test_featured_npc_not_duplicated_with_anchor(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            featured_npc_ids=["npc_anchor_1"],  # same as blueprint anchor
            rng=random.Random(42),
        )
        # Should not have duplicates
        all_ids = [c.npc_id for c in inst.candidates]
        assert all_ids.count("npc_anchor_1") == 1

    def test_featured_npcs_start_active(self):
        bp = _make_blueprint()
        inst = assemble_custom_arc(
            bp,
            "player_1",
            [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
            featured_npc_ids=["rival_npc"],
            rng=random.Random(42),
        )
        rival = next(c for c in inst.candidates if c.npc_id == "rival_npc")
        assert rival.status == "active"
        assert rival.relationship_score == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestCustomArcErrors:
    def test_invalid_phase_count_raises(self):
        bp = _make_blueprint()
        with pytest.raises(CustomArcError, match="out of range"):
            assemble_custom_arc(bp, "player_1", ["combat_1"], rng=random.Random(42))

    def test_unknown_phase_raises(self):
        bp = _make_blueprint()
        with pytest.raises(CustomArcError, match="not in blueprint"):
            assemble_custom_arc(
                bp,
                "player_1",
                ["combat_1", "fake_phase", "social_1"],
                rng=random.Random(42),
            )

    def test_duplicate_phases_raises(self):
        bp = _make_blueprint()
        with pytest.raises(CustomArcError, match="Duplicate"):
            assemble_custom_arc(
                bp,
                "player_1",
                ["combat_1", "combat_1", "social_1"],
                rng=random.Random(42),
            )


# ---------------------------------------------------------------------------
# Rule enforcement in custom arcs
# ---------------------------------------------------------------------------


class TestCustomArcRuleEnforcement:
    def test_must_include_enforced_via_surprise_me(self):
        """When player picks non-combat phases, surprise-me should fix it."""
        bp = _make_blueprint(
            selection_rules={
                "must_include_categories": ["combat"],
                "max_consecutive_same_category": 10,
            }
        )
        rng = random.Random(42)
        # All surprise me — should always include combat
        for _ in range(20):
            inst = assemble_custom_arc(
                bp,
                "player_1",
                [SURPRISE_ME, SURPRISE_ME, SURPRISE_ME],
                rng=rng,
            )
            categories = set()
            for p in inst.phases:
                # Look up the category from the blueprint pool
                for pool_phase in bp.phase_pool:
                    if pool_phase.phase_id == p.phase_id:
                        categories.add(pool_phase.category)
            assert "combat" in categories

    def test_explicit_combat_satisfies_must_include(self):
        bp = _make_blueprint(
            selection_rules={
                "must_include_categories": ["combat"],
                "max_consecutive_same_category": 10,
            }
        )
        inst = assemble_custom_arc(
            bp,
            "player_1",
            ["combat_1", SURPRISE_ME, SURPRISE_ME],
            rng=random.Random(42),
        )
        phase_ids = {p.phase_id for p in inst.phases}
        assert "combat_1" in phase_ids

    def test_all_explicit_valid_selection_works(self):
        """Player picks all phases explicitly, satisfying all rules."""
        bp = _make_blueprint(
            selection_rules={
                "must_include_categories": ["combat"],
                "final_phase_categories": ["combat", "social"],
                "max_consecutive_same_category": 2,
            }
        )
        # combat_1, puzzle_1, social_1 — combat included, ends with social
        inst = assemble_custom_arc(
            bp,
            "player_1",
            ["combat_1", "puzzle_1", "social_1"],
            rng=random.Random(42),
        )
        assert inst.phases[-1].phase_id == "social_1"


# ---------------------------------------------------------------------------
# Integration: load blueprint, assemble custom arc
# ---------------------------------------------------------------------------


class TestCustomArcIntegration:
    @pytest.mark.asyncio
    async def test_load_and_assemble_custom(self):
        from relay.generation.blueprint_loader import clear_cache, get_blueprint

        clear_cache()
        bp = await get_blueprint("inkglass_dark", "guild_trials")
        assert bp is not None

        inst = assemble_custom_arc(
            bp,
            "test_player",
            ["arena_combat", SURPRISE_ME, SURPRISE_ME, "negotiation_trial"],
            featured_npc_ids=["custom_rival"],
            rng=random.Random(42),
        )
        assert isinstance(inst, EventArcInstance)
        assert inst.origin == "custom"
        assert inst.blueprint_id == "guild_trials"

        phase_ids = {p.phase_id for p in inst.phases}
        assert "arena_combat" in phase_ids
        assert "negotiation_trial" in phase_ids

        candidate_ids = {c.npc_id for c in inst.candidates}
        assert "custom_rival" in candidate_ids
