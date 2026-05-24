"""Companion loyalty strain and incapacitation consequences.

At 0 HP a companion withdraws. After combat:
  - gains 1 exhaustion level
  - relationship decreases by dismissal_relationship_modifier
  - loyalty_strain increments by 1

When loyalty_strain >= threshold → confrontation scene triggered.

Design doc: docs/companion system.pdf §Companion Incapacitation
"""

from __future__ import annotations

import logging

from relay.combat.conditions import EXHAUSTION_MAX
from relay.companions.relationship import apply_relationship_change

logger = logging.getLogger(__name__)

DEFAULT_LOYALTY_STRAIN_THRESHOLD = 3
DEFAULT_DISMISSAL_RELATIONSHIP_MODIFIER = -5


def handle_incapacitation(
    *,
    companion: dict,
    companion_data: dict,
    relationships: dict[str, int],
) -> dict:
    """Process companion incapacitation after combat ends.

    Mutates companion dict and relationships dict in-place.

    Returns
    -------
    dict
        Summary of changes applied.
    """
    npc_id = companion["npc_id"]
    old_exhaustion = companion.get("exhaustion_level", 0)
    new_exhaustion = min(old_exhaustion + 1, EXHAUSTION_MAX)
    companion["exhaustion_level"] = new_exhaustion

    modifier = companion_data.get("dismissal_relationship_modifier", DEFAULT_DISMISSAL_RELATIONSHIP_MODIFIER)
    rel_change = apply_relationship_change(relationships, npc_id, modifier)

    old_strain = companion.get("loyalty_strain", 0)
    new_strain = old_strain + 1
    companion["loyalty_strain"] = new_strain

    companion["hp_current"] = 0
    companion["active"] = False

    logger.info(
        "Companion incapacitated",
        extra={
            "npc_id": npc_id,
            "exhaustion": new_exhaustion,
            "relationship_change": modifier,
            "loyalty_strain": new_strain,
        },
    )

    return {
        "npc_id": npc_id,
        "exhaustion_level": new_exhaustion,
        "relationship_old": rel_change["old"],
        "relationship_new": rel_change["new"],
        "loyalty_strain": new_strain,
        "confrontation_triggered": check_confrontation_threshold(companion, companion_data),
    }


def check_confrontation_threshold(
    companion: dict,
    companion_data: dict,
) -> bool:
    """Return True if loyalty_strain has reached the confrontation threshold."""
    threshold = companion_data.get("loyalty_strain_threshold", DEFAULT_LOYALTY_STRAIN_THRESHOLD)
    return companion.get("loyalty_strain", 0) >= threshold


def recover_after_combat(companion: dict) -> dict:
    """Recover companion HP after combat (but keep exhaustion/strain).

    Called when combat ends and companion was incapacitated.
    """
    companion["hp_current"] = companion.get("hp_max", 1)
    companion["active"] = True
    companion["conditions"] = []
    logger.info("Companion recovered after combat", extra={"npc_id": companion["npc_id"]})
    return companion


def clear_exhaustion_on_rest(companion: dict) -> dict:
    """Clear 1 exhaustion level on rest (per design doc)."""
    current = companion.get("exhaustion_level", 0)
    companion["exhaustion_level"] = max(0, current - 1)
    return companion


def apply_dismissal(
    *,
    companion: dict,
    companion_data: dict,
    relationships: dict[str, int],
) -> dict:
    """Process voluntary companion dismissal.

    Returns summary of changes.
    """
    npc_id = companion["npc_id"]
    modifier = companion_data.get("dismissal_relationship_modifier", DEFAULT_DISMISSAL_RELATIONSHIP_MODIFIER)
    rel_change = apply_relationship_change(relationships, npc_id, modifier)

    logger.info(
        "Companion dismissed",
        extra={"npc_id": npc_id, "relationship_change": modifier},
    )

    return {
        "npc_id": npc_id,
        "relationship_old": rel_change["old"],
        "relationship_new": rel_change["new"],
        "farewell_template": companion_data.get("farewell_template"),
    }
