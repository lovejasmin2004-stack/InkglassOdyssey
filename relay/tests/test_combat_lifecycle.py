"""Tests for combat lifecycle hooks."""

from __future__ import annotations

from relay.combat.conditions import add_condition
from relay.combat.lifecycle import on_round_start, on_turn_end, on_turn_start


class TestOnTurnEnd:
    def test_ticks_turn_durations(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=2, duration_unit="turns")
        result = on_turn_end(state)
        assert result[0]["duration_remaining"] == 1

    def test_expires_at_zero(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=1, duration_unit="turns")
        result = on_turn_end(state)
        assert result == []

    def test_does_not_tick_round_unit(self) -> None:
        state = add_condition([], "poisoned", source="spell", duration_remaining=2, duration_unit="rounds")
        result = on_turn_end(state)
        assert result[0]["duration_remaining"] == 2


class TestOnRoundStart:
    def test_ticks_round_durations(self) -> None:
        state = add_condition(
            [],
            "frightened",
            source="dragon",
            duration_remaining=3,
            duration_unit="rounds",
        )
        result = on_round_start(state)
        assert result[0]["duration_remaining"] == 2

    def test_does_not_tick_turn_unit(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=2, duration_unit="turns")
        result = on_round_start(state)
        assert result[0]["duration_remaining"] == 2

    def test_riders_removed_with_parent(self) -> None:
        state = add_condition(
            [],
            "unconscious",
            source="sleep",
            duration_remaining=1,
            duration_unit="rounds",
        )
        result = on_round_start(state)
        assert result == []


class TestOnTurnStart:
    def test_passthrough(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=3, duration_unit="turns")
        result = on_turn_start(state)
        assert result == state

    def test_returns_independent_list(self) -> None:
        state = add_condition([], "poisoned", source="venom", duration_remaining=3, duration_unit="turns")
        result = on_turn_start(state)
        result.clear()
        assert len(state) == 1
