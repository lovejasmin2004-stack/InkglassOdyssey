"""Tests for the Active Effects condition model."""

from __future__ import annotations

from relay.combat.conditions import (
    add_condition,
    has_condition,
    is_incapacitated,
    remove_condition,
    tick_durations,
)


class TestAddCondition:
    def test_add_simple_condition(self) -> None:
        result = add_condition([], "poisoned", source="spider_venom", duration_remaining=3)
        assert len(result) == 1
        assert result[0]["condition_id"] == "poisoned"
        assert result[0]["source"] == "spider_venom"
        assert result[0]["duration_remaining"] == 3
        assert result[0]["rider_of"] is None
        assert result[0]["instance_id"]

    def test_rider_conditions_applied(self) -> None:
        result = add_condition([], "unconscious", source="sleep_spell")
        condition_ids = [c["condition_id"] for c in result]
        assert "unconscious" in condition_ids
        assert "incapacitated" in condition_ids
        assert "prone" in condition_ids

    def test_riders_point_at_parent(self) -> None:
        result = add_condition([], "paralyzed", source="hold_person")
        parent = next(c for c in result if c["condition_id"] == "paralyzed")
        riders = [c for c in result if c["rider_of"] == parent["instance_id"]]
        assert len(riders) == 1
        assert riders[0]["condition_id"] == "incapacitated"

    def test_unknown_condition_noop(self) -> None:
        result = add_condition([], "exploded", source="ghost")
        assert result == []

    def test_exhaustion_levels_recognized(self) -> None:
        result = add_condition([], "exhaustion_3", source="forced_march")
        assert any(c["condition_id"] == "exhaustion_3" for c in result)

    def test_preserves_existing(self) -> None:
        first = add_condition([], "poisoned", source="venom")
        second = add_condition(first, "blinded", source="ash")
        assert len(second) == 2


class TestRemoveCondition:
    def test_remove_by_instance_id(self) -> None:
        state = add_condition([], "poisoned", source="venom")
        instance_id = state[0]["instance_id"]
        result = remove_condition(state, instance_id=instance_id)
        assert result == []

    def test_remove_by_condition_id(self) -> None:
        state = add_condition([], "poisoned", source="venom")
        state = add_condition(state, "blinded", source="ash")
        result = remove_condition(state, condition_id="poisoned")
        assert len(result) == 1
        assert result[0]["condition_id"] == "blinded"

    def test_remove_parent_removes_riders(self) -> None:
        state = add_condition([], "unconscious", source="sleep")
        parent = next(c for c in state if c["condition_id"] == "unconscious")
        result = remove_condition(state, instance_id=parent["instance_id"])
        assert result == []

    def test_remove_unknown_is_noop(self) -> None:
        state = add_condition([], "poisoned", source="venom")
        result = remove_condition(state, condition_id="not_there")
        assert len(result) == 1


class TestTickDurations:
    def test_decrement_matching_unit(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=3, duration_unit="turns")
        result = tick_durations(state, unit="turns")
        assert result[0]["duration_remaining"] == 2

    def test_expiry_removes_condition(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=1, duration_unit="turns")
        result = tick_durations(state, unit="turns")
        assert result == []

    def test_expiry_removes_riders(self) -> None:
        state = add_condition(
            [],
            "unconscious",
            source="sleep",
            duration_remaining=1,
            duration_unit="rounds",
        )
        result = tick_durations(state, unit="rounds")
        assert result == []

    def test_mismatched_unit_untouched(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=3, duration_unit="turns")
        result = tick_durations(state, unit="rounds")
        assert result[0]["duration_remaining"] == 3

    def test_none_duration_untouched(self) -> None:
        state = add_condition(
            [],
            "charmed",
            source="bard",
            duration_remaining=None,
            duration_unit="permanent",
        )
        result = tick_durations(state, unit="turns")
        assert result[0]["duration_remaining"] is None

    def test_riders_not_independently_ticked(self) -> None:
        # Riders inherit duration from parent; ticking the parent ticks the rider transitively.
        state = add_condition(
            [],
            "unconscious",
            source="sleep",
            duration_remaining=2,
            duration_unit="rounds",
        )
        result = tick_durations(state, unit="rounds")
        parent = next(c for c in result if c["condition_id"] == "unconscious")
        assert parent["duration_remaining"] == 1
        # Riders preserved
        assert any(c["condition_id"] == "prone" for c in result)


class TestHelpers:
    def test_has_condition(self) -> None:
        state = add_condition([], "poisoned", source="venom")
        assert has_condition(state, "poisoned")
        assert not has_condition(state, "blinded")

    def test_is_incapacitated_via_rider(self) -> None:
        state = add_condition([], "unconscious", source="sleep")
        assert is_incapacitated(state)

    def test_is_incapacitated_direct(self) -> None:
        state = add_condition([], "incapacitated", source="ghost")
        assert is_incapacitated(state)

    def test_not_incapacitated(self) -> None:
        state = add_condition([], "poisoned", source="venom")
        assert not is_incapacitated(state)
