"""Tests for multi-part typed damage rolls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.combat.damage import (
    apply_resistances,
    parse_formula,
    roll_damage,
    roll_term,
    total_applied,
)


class TestParseFormula:
    def test_simple_dice(self) -> None:
        assert parse_formula("2d6") == (2, 6, 0)

    def test_with_positive_modifier(self) -> None:
        assert parse_formula("1d8+3") == (1, 8, 3)

    def test_with_negative_modifier(self) -> None:
        assert parse_formula("3d6-1") == (3, 6, -1)

    def test_flat_value(self) -> None:
        assert parse_formula("5") == (0, 0, 5)

    def test_negative_flat(self) -> None:
        assert parse_formula("-2") == (0, 0, -2)

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_formula("d6")
        with pytest.raises(ValueError):
            parse_formula("blade")

    def test_whitespace_tolerant(self) -> None:
        assert parse_formula("  1d8 + 3 ") == (1, 8, 3)


class TestRollTerm:
    def test_normal_roll(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[4, 5]):
            result = roll_term({"formula": "2d6", "type": "fire"})
        assert result["dice"] == [4, 5]
        assert result["total"] == 9
        assert result["applied"] == 9
        assert result["type"] == "fire"
        assert result["category"] == "normal"

    def test_critical_doubles_dice(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[4, 5, 6, 1]):
            result = roll_term({"formula": "2d6", "type": "slashing"}, critical=True)
        assert len(result["dice"]) == 4
        assert result["total"] == 4 + 5 + 6 + 1

    def test_modifier_added(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[5]):
            result = roll_term({"formula": "1d8+3", "type": "piercing"})
        assert result["total"] == 8

    def test_flat_damage(self) -> None:
        result = roll_term({"formula": "7", "type": "thunder"})
        assert result["dice"] == []
        assert result["total"] == 7

    def test_negative_total_clamped_to_zero(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[1]):
            result = roll_term({"formula": "1d4-10", "type": "fire"})
        assert result["total"] == 0


class TestApplyResistances:
    def _term(self, dtype: str, total: int) -> dict:
        return {
            "formula": "1d8",
            "type": dtype,
            "dice": [],
            "modifier": 0,
            "total": total,
            "applied": total,
            "category": "normal",
        }

    def test_resistance_halves(self) -> None:
        terms = [self._term("fire", 10)]
        result = apply_resistances(terms, resistances=["fire"])
        assert result[0]["applied"] == 5
        assert result[0]["category"] == "resistant"

    def test_vulnerability_doubles(self) -> None:
        terms = [self._term("cold", 7)]
        result = apply_resistances(terms, vulnerabilities=["cold"])
        assert result[0]["applied"] == 14
        assert result[0]["category"] == "vulnerable"

    def test_immunity_zeroes(self) -> None:
        terms = [self._term("poison", 20)]
        result = apply_resistances(terms, immunities=["poison"])
        assert result[0]["applied"] == 0
        assert result[0]["category"] == "immune"

    def test_immunity_beats_vulnerability(self) -> None:
        terms = [self._term("fire", 10)]
        result = apply_resistances(terms, vulnerabilities=["fire"], immunities=["fire"])
        assert result[0]["applied"] == 0
        assert result[0]["category"] == "immune"

    def test_resistance_and_vulnerability_cancel(self) -> None:
        terms = [self._term("fire", 10)]
        result = apply_resistances(terms, resistances=["fire"], vulnerabilities=["fire"])
        assert result[0]["applied"] == 10
        assert result[0]["category"] == "normal"

    def test_per_term_independence(self) -> None:
        """A flame-tongue strike: piercing isn't reduced when target resists fire."""
        terms = [self._term("piercing", 8), self._term("fire", 7)]
        result = apply_resistances(terms, resistances=["fire"])
        assert result[0]["applied"] == 8
        assert result[1]["applied"] == 3

    def test_resistance_rounds_down(self) -> None:
        terms = [self._term("fire", 7)]
        result = apply_resistances(terms, resistances=["fire"])
        assert result[0]["applied"] == 3

    def test_no_modifiers(self) -> None:
        terms = [self._term("fire", 6)]
        result = apply_resistances(terms)
        assert result[0]["applied"] == 6
        assert result[0]["category"] == "normal"

    def test_does_not_mutate_input(self) -> None:
        terms = [self._term("fire", 10)]
        apply_resistances(terms, resistances=["fire"])
        assert terms[0]["applied"] == 10


class TestRollDamage:
    def test_multi_part(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[4, 5, 3]):
            result = roll_damage(
                [
                    {"formula": "1d8", "type": "piercing"},
                    {"formula": "2d6", "type": "fire"},
                ]
            )
        assert len(result) == 2
        assert result[0]["type"] == "piercing"
        assert result[0]["total"] == 4
        assert result[1]["type"] == "fire"
        assert result[1]["total"] == 8

    def test_critical_propagates(self) -> None:
        with patch("relay.combat.damage.random.randint", side_effect=[4, 5, 6, 1]):
            result = roll_damage(
                [{"formula": "2d6", "type": "slashing"}],
                critical=True,
            )
        assert len(result[0]["dice"]) == 4


class TestTotalApplied:
    def test_sum(self) -> None:
        terms = [
            {
                "formula": "",
                "type": "fire",
                "dice": [],
                "modifier": 0,
                "total": 5,
                "applied": 5,
                "category": "normal",
            },
            {
                "formula": "",
                "type": "cold",
                "dice": [],
                "modifier": 0,
                "total": 7,
                "applied": 3,
                "category": "resistant",
            },
        ]
        assert total_applied(terms) == 8

    def test_empty(self) -> None:
        assert total_applied([]) == 0
