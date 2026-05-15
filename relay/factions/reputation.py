"""Faction reputation — standing changes, tier resolution, propagation.

Design doc: docs/faction system.pdf

Standing is -100 to 100 per player per faction, stored in character.faction_standing.
Changes propagate automatically (single-hop, not transitive):
  - Allied factions: +50% of the delta
  - Rival factions: -25% of the delta (floored to integers)

Propagation is single-hop: if A's ally is B and B's ally is C, changing A's
standing propagates to B but NOT to C.  The design doc does not specify
transitive propagation.
"""

from __future__ import annotations

import logging
import math

from relay.economy.pricing import faction_tier

logger = logging.getLogger(__name__)

_STANDING_MIN = -100
_STANDING_MAX = 100

_ALLY_PROPAGATION = 0.50
_RIVAL_PROPAGATION = -0.25


def clamp_standing(value: int) -> int:
    return max(_STANDING_MIN, min(_STANDING_MAX, value))


def resolve_tier(
    standing: int,
    thresholds: dict[str, int] | None = None,
) -> str:
    """Resolve the tier label for a standing value.

    Uses custom per-faction thresholds if provided, otherwise defaults.
    """
    if thresholds is None:
        return faction_tier(standing)

    standing = clamp_standing(standing)
    if standing >= thresholds.get("allied", 51):
        return "allied"
    if standing >= thresholds.get("friendly", 1):
        return "friendly"
    if standing >= thresholds.get("neutral", 0):
        return "neutral"
    if standing >= thresholds.get("unfriendly", -1):
        return "unfriendly"
    return "hostile"


def apply_standing_change(
    faction_standing: dict[str, int],
    faction_id: str,
    delta: int,
    faction_registry: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Apply a standing change with automatic propagation.

    Propagation is single-hop: only the target faction's direct allies and
    rivals are affected.  Allies-of-allies are not touched.

    Parameters
    ----------
    faction_standing : dict[str, int]
        The character's current faction_standing dict. Modified in place.
    faction_id : str
        The faction whose standing is changing directly.
    delta : int
        The signed change to apply.  Clamped to the effective range
        (+-200) so the result stays in [-100, 100].
    faction_registry : dict[str, dict] | None
        Map of faction_id to faction definition (must have allied_factions,
        rival_factions; optionally reputation_thresholds for custom tier
        boundaries).  If None, no propagation occurs.
        Loaded from server-side content files via world.content_loader.

    Returns
    -------
    dict[str, dict]
        A report of all changes: {faction_id: {"old": int, "new": int, "tier": str}}.
    """
    changes: dict[str, dict] = {}

    # Clamp delta to effective range -- any value beyond +-200 has the
    # same effect since standing is bounded to [-100, 100].
    delta = max(-2 * _STANDING_MAX, min(2 * _STANDING_MAX, delta))

    old = faction_standing.get(faction_id, 0)
    new = clamp_standing(old + delta)
    faction_standing[faction_id] = new

    # Resolve tier using per-faction custom thresholds when available
    faction_def = faction_registry.get(faction_id) if faction_registry else None
    thresholds = faction_def.get("reputation_thresholds") if faction_def else None

    changes[faction_id] = {
        "old": old,
        "new": new,
        "tier": resolve_tier(new, thresholds),
        "source": "direct",
    }

    if faction_def is None:
        logger.info(
            "Faction standing changed",
            extra={
                "faction_id": faction_id,
                "delta": delta,
                "changes": changes,
            },
        )
        return changes

    # Single-hop propagation: direct allies and rivals only
    for ally_id in faction_def.get("allied_factions", []):
        ally_delta = math.floor(delta * _ALLY_PROPAGATION)
        if ally_delta == 0:
            continue
        ally_old = faction_standing.get(ally_id, 0)
        ally_new = clamp_standing(ally_old + ally_delta)
        faction_standing[ally_id] = ally_new
        ally_def = faction_registry.get(ally_id)
        ally_thresholds = ally_def.get("reputation_thresholds") if ally_def else None
        changes[ally_id] = {
            "old": ally_old,
            "new": ally_new,
            "tier": resolve_tier(ally_new, ally_thresholds),
            "source": "allied_propagation",
        }

    for rival_id in faction_def.get("rival_factions", []):
        rival_delta = math.floor(delta * _RIVAL_PROPAGATION)
        if rival_delta == 0:
            continue
        rival_old = faction_standing.get(rival_id, 0)
        rival_new = clamp_standing(rival_old + rival_delta)
        faction_standing[rival_id] = rival_new
        rival_def = faction_registry.get(rival_id)
        rival_thresholds = rival_def.get("reputation_thresholds") if rival_def else None
        changes[rival_id] = {
            "old": rival_old,
            "new": rival_new,
            "tier": resolve_tier(rival_new, rival_thresholds),
            "source": "rival_propagation",
        }

    logger.info(
        "Faction standing changed",
        extra={
            "faction_id": faction_id,
            "delta": delta,
            "changes": changes,
        },
    )

    return changes
