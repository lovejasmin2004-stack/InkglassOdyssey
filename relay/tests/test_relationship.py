"""Tests for the relationship threshold tier system."""

from __future__ import annotations

import pytest

from relay.companions.relationship import (
    DEFAULT_RELATIONSHIP_TIERS,
    RELATIONSHIP_MAX,
    RELATIONSHIP_MIN,
    apply_relationship_change,
    clamp_relationship,
    resolve_relationship_tier,
)


class TestClamp:
    def test_within_bounds(self):
        assert clamp_relationship(50) == 50

    def test_at_min(self):
        assert clamp_relationship(-100) == -100

    def test_at_max(self):
        assert clamp_relationship(100) == 100

    def test_below_min(self):
        assert clamp_relationship(-500) == RELATIONSHIP_MIN

    def test_above_max(self):
        assert clamp_relationship(500) == RELATIONSHIP_MAX

    def test_zero(self):
        assert clamp_relationship(0) == 0


class TestResolveTier:
    def test_hostile(self):
        assert resolve_relationship_tier(-100) == "hostile"
        assert resolve_relationship_tier(-76) == "hostile"

    def test_wary(self):
        assert resolve_relationship_tier(-50) == "wary"
        assert resolve_relationship_tier(-11) == "wary"

    def test_hostile_is_catchall_below_wary(self):
        assert resolve_relationship_tier(-51) == "hostile"
        assert resolve_relationship_tier(-75) == "hostile"

    def test_neutral(self):
        assert resolve_relationship_tier(-10) == "neutral"
        assert resolve_relationship_tier(0) == "neutral"
        assert resolve_relationship_tier(10) == "neutral"

    def test_acquaintance(self):
        assert resolve_relationship_tier(11) == "acquaintance"
        assert resolve_relationship_tier(35) == "acquaintance"

    def test_friendly(self):
        assert resolve_relationship_tier(36) == "friendly"
        assert resolve_relationship_tier(60) == "friendly"

    def test_trusted(self):
        assert resolve_relationship_tier(61) == "trusted"
        assert resolve_relationship_tier(85) == "trusted"

    def test_bonded(self):
        assert resolve_relationship_tier(86) == "bonded"
        assert resolve_relationship_tier(100) == "bonded"

    def test_out_of_bounds_clamped(self):
        assert resolve_relationship_tier(-500) == "hostile"
        assert resolve_relationship_tier(500) == "bonded"

    def test_custom_thresholds(self):
        custom = {
            "hostile": -90,
            "wary": -60,
            "neutral": -20,
            "acquaintance": 5,
            "friendly": 30,
            "trusted": 55,
            "bonded": 80,
        }
        assert resolve_relationship_tier(5, custom) == "acquaintance"
        assert resolve_relationship_tier(-20, custom) == "neutral"
        assert resolve_relationship_tier(-25, custom) == "wary"
        assert resolve_relationship_tier(80, custom) == "bonded"
        assert resolve_relationship_tier(-61, custom) == "hostile"
        assert resolve_relationship_tier(-60, custom) == "wary"

    def test_custom_thresholds_partial_fallback(self):
        custom = {"bonded": 90}
        assert resolve_relationship_tier(90, custom) == "bonded"
        assert resolve_relationship_tier(86, custom) == "trusted"

    def test_every_boundary_in_default_tiers(self):
        # hostile is the catch-all below wary, not a comparison boundary
        boundaries = sorted(DEFAULT_RELATIONSHIP_TIERS.items(), key=lambda x: x[1])
        for tier_name, threshold in boundaries:
            if tier_name == "hostile":
                continue
            assert resolve_relationship_tier(threshold) == tier_name
            prev_tier = resolve_relationship_tier(threshold - 1)
            assert prev_tier != tier_name


class TestApplyChange:
    def test_basic_positive(self):
        rels: dict[str, int] = {"npc_a": 50}
        result = apply_relationship_change(rels, "npc_a", 10)
        assert rels["npc_a"] == 60
        assert result["old"] == 50
        assert result["new"] == 60
        assert result["delta"] == 10

    def test_basic_negative(self):
        rels: dict[str, int] = {"npc_a": 50}
        result = apply_relationship_change(rels, "npc_a", -20)
        assert rels["npc_a"] == 30
        assert result["old"] == 50
        assert result["new"] == 30

    def test_clamps_at_max(self):
        rels: dict[str, int] = {"npc_a": 90}
        result = apply_relationship_change(rels, "npc_a", 50)
        assert rels["npc_a"] == 100
        assert result["new"] == 100

    def test_clamps_at_min(self):
        rels: dict[str, int] = {"npc_a": -90}
        result = apply_relationship_change(rels, "npc_a", -50)
        assert rels["npc_a"] == -100
        assert result["new"] == -100

    def test_new_npc_defaults_to_zero(self):
        rels: dict[str, int] = {}
        result = apply_relationship_change(rels, "new_npc", 15)
        assert rels["new_npc"] == 15
        assert result["old"] == 0
        assert result["new"] == 15

    def test_tier_changed_flag(self):
        rels: dict[str, int] = {"npc_a": 60}
        result = apply_relationship_change(rels, "npc_a", 1)
        assert result["tier_changed"] is True
        assert result["old_tier"] == "friendly"
        assert result["new_tier"] == "trusted"

    def test_tier_unchanged_flag(self):
        rels: dict[str, int] = {"npc_a": 50}
        result = apply_relationship_change(rels, "npc_a", 5)
        assert result["tier_changed"] is False
        assert result["old_tier"] == "friendly"
        assert result["new_tier"] == "friendly"

    def test_zero_delta(self):
        rels: dict[str, int] = {"npc_a": 50}
        result = apply_relationship_change(rels, "npc_a", 0)
        assert rels["npc_a"] == 50
        assert result["old"] == 50
        assert result["new"] == 50
        assert result["tier_changed"] is False

    def test_mutates_dict_in_place(self):
        rels: dict[str, int] = {"npc_a": 50}
        original_id = id(rels)
        apply_relationship_change(rels, "npc_a", 10)
        assert id(rels) == original_id
