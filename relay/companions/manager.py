"""Companion recruitment and roster management.

Handles recruitment validation (affection threshold, companion limit,
conditions), adding/removing companions from the character's companion list.

Design doc: docs/companion system.pdf
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_ACTIVE_COMPANIONS = 1


class RecruitmentError(Exception):
    pass


class CompanionLimitError(RecruitmentError):
    pass


class AffectionTooLowError(RecruitmentError):
    pass


class ConditionNotMetError(RecruitmentError):
    pass


class AlreadyRecruitedError(RecruitmentError):
    pass


def validate_recruitment(
    *,
    npc_id: str,
    companion_data: dict,
    relationship_score: int,
    current_companions: list[dict],
    max_active_companions: int = DEFAULT_MAX_ACTIVE_COMPANIONS,
    world_flags: dict[str, bool] | None = None,
) -> None:
    """Validate that a companion can be recruited.

    Raises RecruitmentError subclasses on failure.
    """
    if any(c["npc_id"] == npc_id for c in current_companions):
        raise AlreadyRecruitedError(f"{npc_id} is already a companion")

    active = [c for c in current_companions if c.get("active", True)]
    if len(active) >= max_active_companions:
        raise CompanionLimitError(f"Companion limit reached ({max_active_companions})")

    recruitment = companion_data.get("recruitment", {})
    threshold = recruitment.get("affection_threshold", 0)
    if relationship_score < threshold:
        raise AffectionTooLowError(f"Relationship score {relationship_score} below threshold {threshold}")

    conditions = recruitment.get("recruitment_conditions") or []
    flags = world_flags or {}
    for cond in conditions:
        if not flags.get(cond, False):
            raise ConditionNotMetError(f"Condition not met: {cond}")


def create_companion_entry(
    *,
    npc_id: str,
    companion_data: dict,
    npc_hp_max: int,
) -> dict:
    """Build the companion state dict to store in character.companions."""
    combat_profile = companion_data.get("combat_profile", {})
    return {
        "npc_id": npc_id,
        "hp_current": npc_hp_max,
        "hp_max": npc_hp_max,
        "conditions": [],
        "exhaustion_level": 0,
        "loyalty_strain": 0,
        "behavior_type": combat_profile.get("behavior_type", "defensive"),
        "active": True,
    }


def add_companion(
    companions: list[dict],
    entry: dict,
) -> list[dict]:
    """Append a companion entry to the list. Returns updated list."""
    companions.append(entry)
    logger.info("Companion recruited", extra={"npc_id": entry["npc_id"]})
    return companions


def remove_companion(companions: list[dict], npc_id: str) -> list[dict]:
    """Remove a companion by npc_id. Returns updated list."""
    result = [c for c in companions if c["npc_id"] != npc_id]
    logger.info("Companion removed", extra={"npc_id": npc_id})
    return result


def find_companion(companions: list[dict], npc_id: str) -> dict | None:
    """Find a companion entry by npc_id, or None."""
    for c in companions:
        if c["npc_id"] == npc_id:
            return c
    return None
