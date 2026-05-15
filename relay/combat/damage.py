"""Multi-part typed damage rolls.

Damage is a list of typed terms (e.g., a flame-tongue strike is
[{"formula": "1d8", "type": "slashing"}, {"formula": "2d6", "type": "fire"}]).
Each term rolls independently. Critical hits double the dice (not the
modifier). Resistance / vulnerability / immunity apply per-term post-roll
(Invariant #21).

Pattern adopted from Foundry dnd5e's DamageRoll: each term is independent so
that a fire-resistant target reduces only the fire portion.
"""

from __future__ import annotations

import logging
import random
import re

logger = logging.getLogger(__name__)

DamageTerm = dict[str, str]
"""{"formula": "NdM" or "NdM+K" or "K", "type": damage_type}."""

RolledTerm = dict[str, object]
"""{"formula", "type", "dice": [...], "modifier", "total", "applied", "category"}."""


_DICE_RE = re.compile(r"^\s*(\d+)d(\d+)\s*(?:([+-])\s*(\d+))?\s*$")
_FLAT_RE = re.compile(r"^\s*([+-]?\d+)\s*$")


def parse_formula(formula: str) -> tuple[int, int, int]:
    """Parse 'NdM', 'NdM+K', or flat 'K' into (n_dice, die_size, flat_mod).

    Flat formulas return (0, 0, K). Raises ValueError on malformed input.
    """
    formula = formula.strip()
    m = _DICE_RE.match(formula)
    if m:
        n = int(m.group(1))
        size = int(m.group(2))
        sign = m.group(3) or "+"
        mod = int(m.group(4) or 0)
        if sign == "-":
            mod = -mod
        return n, size, mod
    m = _FLAT_RE.match(formula)
    if m:
        return 0, 0, int(m.group(1))
    raise ValueError(f"invalid damage formula: {formula!r}")


def _roll_dice(n: int, size: int) -> list[int]:
    if n <= 0 or size <= 0:
        return []
    return [random.randint(1, size) for _ in range(n)]


def roll_term(term: DamageTerm, *, critical: bool = False) -> RolledTerm:
    """Roll a single typed damage term. On a critical, dice count is doubled."""
    formula = term["formula"]
    damage_type = term["type"]
    n, size, mod = parse_formula(formula)

    rolled_n = n * 2 if critical else n
    dice = _roll_dice(rolled_n, size)
    total = sum(dice) + mod

    return {
        "formula": formula,
        "type": damage_type,
        "dice": dice,
        "modifier": mod,
        "total": max(0, total),
        "applied": max(0, total),
        "category": "normal",
    }


def roll_damage(
    terms: list[DamageTerm],
    *,
    critical: bool = False,
) -> list[RolledTerm]:
    """Roll every term in the damage expression. Returns a parallel list."""
    return [roll_term(t, critical=critical) for t in terms]


def apply_resistances(
    rolled_terms: list[RolledTerm],
    *,
    resistances: list[str] | None = None,
    vulnerabilities: list[str] | None = None,
    immunities: list[str] | None = None,
) -> list[RolledTerm]:
    """Apply per-type resistance / vulnerability / immunity post-roll.

    Resistance halves (round down). Vulnerability doubles. Immunity zeroes.
    Immunity wins if multiple apply. Returns new list with `applied` and
    `category` updated; original `total` preserved for transparency.
    """
    resistances_s = set(resistances or [])
    vulnerabilities_s = set(vulnerabilities or [])
    immunities_s = set(immunities or [])

    out: list[RolledTerm] = []
    for term in rolled_terms:
        new_term = dict(term)
        dtype = str(term["type"])
        total = int(term["total"])

        if dtype in immunities_s:
            new_term["applied"] = 0
            new_term["category"] = "immune"
        elif dtype in vulnerabilities_s and dtype in resistances_s:
            new_term["applied"] = total
            new_term["category"] = "normal"
        elif dtype in vulnerabilities_s:
            new_term["applied"] = total * 2
            new_term["category"] = "vulnerable"
        elif dtype in resistances_s:
            new_term["applied"] = total // 2
            new_term["category"] = "resistant"
        else:
            new_term["applied"] = total
            new_term["category"] = "normal"
        out.append(new_term)
    return out


def total_applied(rolled_terms: list[RolledTerm]) -> int:
    """Sum of post-resistance damage across all terms. Minimum 0."""
    return max(0, sum(int(t["applied"]) for t in rolled_terms))
