"""NPC relationship scores — clamping, named tiers, centralized mutation.

Mirrors the pattern established by relay/factions/reputation.py for faction
standing.  Relationship scores are per-NPC integers on Character.relationships,
clamped to [RELATIONSHIP_MIN, RELATIONSHIP_MAX].

Tier labels provide shared vocabulary for content authors (NPC secrets,
scenario prerequisites, ambient dialogue) and mechanical gating (companion
recruitment, secret reveals).

Design doc: docs/companion system.pdf
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

RELATIONSHIP_MIN = -100
RELATIONSHIP_MAX = 100

DEFAULT_RELATIONSHIP_TIERS: dict[str, int] = {
    "hostile": -76,
    "wary": -50,
    "neutral": -10,
    "acquaintance": 11,
    "friendly": 36,
    "trusted": 61,
    "bonded": 86,
}


def clamp_relationship(value: int) -> int:
    return max(RELATIONSHIP_MIN, min(RELATIONSHIP_MAX, value))


def resolve_relationship_tier(
    score: int,
    custom_thresholds: dict[str, int] | None = None,
) -> str:
    """Resolve the tier label for a relationship score.

    Uses custom per-NPC thresholds if provided, otherwise
    :data:`DEFAULT_RELATIONSHIP_TIERS`.
    """
    t = custom_thresholds if custom_thresholds is not None else DEFAULT_RELATIONSHIP_TIERS
    score = clamp_relationship(score)
    if score >= t.get("bonded", DEFAULT_RELATIONSHIP_TIERS["bonded"]):
        return "bonded"
    if score >= t.get("trusted", DEFAULT_RELATIONSHIP_TIERS["trusted"]):
        return "trusted"
    if score >= t.get("friendly", DEFAULT_RELATIONSHIP_TIERS["friendly"]):
        return "friendly"
    if score >= t.get("acquaintance", DEFAULT_RELATIONSHIP_TIERS["acquaintance"]):
        return "acquaintance"
    if score >= t.get("neutral", DEFAULT_RELATIONSHIP_TIERS["neutral"]):
        return "neutral"
    if score >= t.get("wary", DEFAULT_RELATIONSHIP_TIERS["wary"]):
        return "wary"
    return "hostile"


def apply_relationship_change(
    relationships: dict[str, int],
    npc_id: str,
    delta: int,
) -> dict:
    """Apply a relationship score change with clamping.

    Mutates ``relationships`` in-place (matching the convention used by
    ``factions.reputation.apply_standing_change``).

    Returns a summary dict with old/new values and tier labels.
    """
    old = relationships.get(npc_id, 0)
    new = clamp_relationship(old + delta)
    relationships[npc_id] = new

    old_tier = resolve_relationship_tier(old)
    new_tier = resolve_relationship_tier(new)

    logger.info(
        "Relationship changed",
        extra={
            "npc_id": npc_id,
            "delta": delta,
            "old": old,
            "new": new,
            "old_tier": old_tier,
            "new_tier": new_tier,
        },
    )

    return {
        "npc_id": npc_id,
        "old": old,
        "new": new,
        "delta": delta,
        "old_tier": old_tier,
        "new_tier": new_tier,
        "tier_changed": old_tier != new_tier,
    }
