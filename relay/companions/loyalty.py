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

logger = logging.getLogger(__name__)

DEFAULT_LOYALTY_STRAIN_THRESHOLD = 3
DEFAULT_DISMISSAL_RELATIONSHIP_MODIFIER = -5

_RELATIONSHIP_MIN = -100
_RELATIONSHIP_MAX = 100


def _clamp_relationship(value: int) -> int:
    """Clamp a relationship score to [-100, 100]."""
    return max(_RELATIONSHIP_MIN, min(_RELATIONSHIP_MAX, value))


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
    old_relationship = relationships.get(npc_id, 0)
    new_relationship = _clamp_relationship(old_relationship + modifier)
    relationships[npc_id] = new_relationship

    old_strain = companion.get("loyalty_strain", 0)
    new_strain = old_strain + 1
    companion["loyalty_strain"] = new_strain

    companion["hp_current"] = 0
    companion["active"] = False

    confrontation = check_confrontation_threshold(companion, companion_data)
    if confrontation:
        logger.warning(
            "Confrontation threshold reached",
            extra={"npc_id": npc_id, "loyalty_strain": new_strain},
        )

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
        "relationship_old": old_relationship,
        "relationship_new": new_relationship,
        "loyalty_strain": new_strain,
        "confrontation_triggered": confrontation,
        # TODO(phase-3): wire confrontation_scene_id to scenario system
        "confrontation_scene_id": companion_data.get("confrontation_scene_id") if confrontation else None,
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
    old_relationship = relationships.get(npc_id, 0)
    new_relationship = _clamp_relationship(old_relationship + modifier)
    relationships[npc_id] = new_relationship

    logger.info(
        "Companion dismissed",
        extra={"npc_id": npc_id, "relationship_change": modifier},
    )

    return {
        "npc_id": npc_id,
        "relationship_old": old_relationship,
        "relationship_new": new_relationship,
        "farewell_template": companion_data.get("farewell_template"),
    }
