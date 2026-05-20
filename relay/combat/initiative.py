"""Initiative resolution for multi-participant combat.

In prose RP combat the player always acts first (they wrote the prose).
Initiative determines NPC response order in multi-participant encounters.
"""

from __future__ import annotations

import logging

from relay.checks.resolver import ability_modifier, roll_d20

logger = logging.getLogger(__name__)


def roll_initiative(
    participant_id: str,
    ability_scores: dict[str, int],
) -> dict:
    """Roll initiative: d20 + DEX modifier.

    (#1) Uses shared roll_d20 for consistency with checks and combat.
    """
    dex_mod = ability_modifier(ability_scores.get("dexterity", 10))
    roll, dice = roll_d20("straight")
    total = roll + dex_mod

    return {
        "participant_id": participant_id,
        "roll": roll,
        "dice": dice,
        "dex_modifier": dex_mod,
        "total": total,
    }


def determine_turn_order(participants: list[dict]) -> list[dict]:
    """Sort participants by initiative total (highest first).

    participants: list of {id, ability_scores} dicts.
    Returns sorted list of initiative results.
    """
    results = []
    for p in participants:
        result = roll_initiative(p["id"], p["ability_scores"])
        results.append(result)

    results.sort(key=lambda r: (r["total"], r["dex_modifier"]), reverse=True)

    logger.info(
        "Initiative order determined",
        extra={"order": [r["participant_id"] for r in results]},
    )
    return results
