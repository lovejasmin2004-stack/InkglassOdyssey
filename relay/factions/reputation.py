"""Faction reputation — standing changes, tier resolution, propagation.

Design doc: docs/faction system.pdf

Standing is -100 to 100 per player per faction, stored in character.faction_standing.
Changes propagate automatically (single-hop, not transitive):
  - Allied factions: +50% of the delta (truncated toward zero)
  - Rival factions: -25% of the delta (truncated toward zero)

Propagation is single-hop: if A's ally is B and B's ally is C, changing A's
standing propagates to B but NOT to C.  The design doc does not specify
transitive propagation.

Rounding
--------
``math.trunc`` (truncation toward zero) is used for propagated deltas to
ensure symmetric behaviour.  With ``math.floor``, gaining +5 standing would
cost a rival -2 but losing -5 would only give the rival +1 — an unbalanced
ratchet effect.  ``math.trunc`` produces equal magnitudes in both directions.

Propagation cap
---------------
Individual propagated deltas are capped at +-_MAX_PROPAGATED_DELTA to prevent
single large events (e.g. a +80 quest reward) from radically shifting the
entire faction graph in one tick.

Delta range
-----------
The HTTP endpoint constrains delta to [-100, 100] for player-facing API calls.
Internally, ``apply_standing_change`` accepts [-200, 200] as headroom for
quest rewards, narrative events, and other relay-initiated changes that may
legitimately exceed the endpoint limit.

Faction graph notes
-------------------
Some factions may intentionally have no allies (e.g. witches_circle in
inkglass_dark).  These "isolated" factions can only gain standing through
direct player actions, never via allied propagation.  This is a deliberate
design choice to make certain factions harder to befriend.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

_STANDING_MIN = -100
_STANDING_MAX = 100

_ALLY_PROPAGATION = 0.50
_RIVAL_PROPAGATION = -0.25

# Cap on any single propagated delta to prevent large events from
# dominating the entire faction graph.
_MAX_PROPAGATED_DELTA = 20

# Default tier boundaries used when a faction does not define custom
# ``reputation_thresholds``.  These are the single source of truth for
# the tier ranges listed in docs/faction system.pdf §Standing Tiers.
#
# Each value is the *inclusive lower bound* of its tier:
#   hostile  : standing < unfriendly
#   unfriendly: unfriendly <= standing < neutral
#   neutral  : neutral   <= standing < friendly
#   friendly : friendly  <= standing < allied
#   allied   : standing >= allied
#
# The ``hostile`` key participates in the ordering validator
# (``ReputationThresholds._check_ordering``) but is not used as a
# comparison boundary — hostile is the catch-all below unfriendly.
DEFAULT_THRESHOLDS: dict[str, int] = {
    "hostile": -51,
    "unfriendly": -50,
    "neutral": -10,
    "friendly": 11,
    "allied": 51,
}


def clamp_standing(value: int) -> int:
    return max(_STANDING_MIN, min(_STANDING_MAX, value))


def resolve_tier(
    standing: int,
    thresholds: dict[str, int] | None = None,
) -> str:
    """Resolve the tier label for a standing value.

    Uses custom per-faction thresholds if provided, otherwise
    :data:`DEFAULT_THRESHOLDS`.  Missing keys in a custom dict fall back
    to the corresponding default value.

    Note: the ``hostile`` threshold value participates in the ordering
    validator (``ReputationThresholds._check_ordering``) to ensure
    threshold consistency, but is not used as a comparison boundary
    here — hostile is the catch-all tier for any standing below the
    ``unfriendly`` boundary.
    """
    t = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    standing = clamp_standing(standing)
    if standing >= t.get("allied", DEFAULT_THRESHOLDS["allied"]):
        return "allied"
    if standing >= t.get("friendly", DEFAULT_THRESHOLDS["friendly"]):
        return "friendly"
    if standing >= t.get("neutral", DEFAULT_THRESHOLDS["neutral"]):
        return "neutral"
    if standing >= t.get("unfriendly", DEFAULT_THRESHOLDS["unfriendly"]):
        return "unfriendly"
    return "hostile"


def _cap_propagated(delta: int) -> int:
    """Clamp a propagated delta to the propagation cap."""
    return max(-_MAX_PROPAGATED_DELTA, min(_MAX_PROPAGATED_DELTA, delta))


def apply_standing_change(
    faction_standing: dict[str, int],
    faction_id: str,
    delta: int,
    faction_registry: dict[str, dict] | None = None,
    *,
    reason: str = "",
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
        (+-200) so the result stays in [-100, 100].  The HTTP endpoint
        constrains to +-100; the wider internal range exists for quest
        rewards and narrative events.
    faction_registry : dict[str, dict] | None
        Map of faction_id to faction definition (must have allied_factions,
        rival_factions; optionally reputation_thresholds for custom tier
        boundaries).  If None, no propagation occurs.
        Loaded from server-side content files via world.content_loader.
    reason : str
        Human-readable reason for the change (e.g. "quest_completion").
        Included in the change report for audit logging.

    Returns
    -------
    dict[str, dict]
        A report of all changes: ``{faction_id: {"old", "new", "tier",
        "old_tier", "tier_changed", "source", "reason"}}``.
    """
    changes: dict[str, dict] = {}

    # Clamp delta to effective range — any value beyond +-200 has the
    # same effect since standing is bounded to [-100, 100].
    delta = max(-2 * _STANDING_MAX, min(2 * _STANDING_MAX, delta))

    old = faction_standing.get(faction_id, 0)
    new = clamp_standing(old + delta)
    faction_standing[faction_id] = new

    # Resolve tier using per-faction custom thresholds when available
    faction_def = faction_registry.get(faction_id) if faction_registry else None
    thresholds = faction_def.get("reputation_thresholds") if faction_def else None

    old_tier = resolve_tier(old, thresholds)
    new_tier = resolve_tier(new, thresholds)

    changes[faction_id] = {
        "old": old,
        "new": new,
        "tier": new_tier,
        "old_tier": old_tier,
        "tier_changed": old_tier != new_tier,
        "source": "direct",
        "reason": reason,
    }

    if faction_def is None:
        logger.info(
            "Faction standing changed",
            extra={
                "faction_id": faction_id,
                "delta": delta,
                "reason": reason,
                "changes": changes,
            },
        )
        return changes

    # Single-hop propagation: direct allies and rivals only.
    # Propagated deltas use math.trunc (toward zero) for symmetry, and are
    # capped at +-_MAX_PROPAGATED_DELTA.
    for ally_id in faction_def.get("allied_factions", []):
        # Skip references to factions not in the registry to prevent
        # phantom standings from typos or deleted factions.
        if faction_registry and ally_id not in faction_registry:
            logger.warning(
                "Skipping propagation to unknown ally faction",
                extra={"source_faction": faction_id, "unknown_ally": ally_id},
            )
            continue
        ally_delta = _cap_propagated(math.trunc(delta * _ALLY_PROPAGATION))
        if ally_delta == 0:
            continue
        ally_old = faction_standing.get(ally_id, 0)
        ally_new = clamp_standing(ally_old + ally_delta)
        faction_standing[ally_id] = ally_new
        ally_def = faction_registry.get(ally_id)
        ally_thresholds = ally_def.get("reputation_thresholds") if ally_def else None
        ally_old_tier = resolve_tier(ally_old, ally_thresholds)
        ally_new_tier = resolve_tier(ally_new, ally_thresholds)
        changes[ally_id] = {
            "old": ally_old,
            "new": ally_new,
            "tier": ally_new_tier,
            "old_tier": ally_old_tier,
            "tier_changed": ally_old_tier != ally_new_tier,
            "source": "allied_propagation",
            "reason": reason,
        }

    for rival_id in faction_def.get("rival_factions", []):
        # Skip references to factions not in the registry to prevent
        # phantom standings from typos or deleted factions.
        if faction_registry and rival_id not in faction_registry:
            logger.warning(
                "Skipping propagation to unknown rival faction",
                extra={"source_faction": faction_id, "unknown_rival": rival_id},
            )
            continue
        rival_delta = _cap_propagated(math.trunc(delta * _RIVAL_PROPAGATION))
        if rival_delta == 0:
            continue
        rival_old = faction_standing.get(rival_id, 0)
        rival_new = clamp_standing(rival_old + rival_delta)
        faction_standing[rival_id] = rival_new
        rival_def = faction_registry.get(rival_id)
        rival_thresholds = rival_def.get("reputation_thresholds") if rival_def else None
        rival_old_tier = resolve_tier(rival_old, rival_thresholds)
        rival_new_tier = resolve_tier(rival_new, rival_thresholds)
        changes[rival_id] = {
            "old": rival_old,
            "new": rival_new,
            "tier": rival_new_tier,
            "old_tier": rival_old_tier,
            "tier_changed": rival_old_tier != rival_new_tier,
            "source": "rival_propagation",
            "reason": reason,
        }

    logger.info(
        "Faction standing changed",
        extra={
            "faction_id": faction_id,
            "delta": delta,
            "reason": reason,
            "changes": changes,
        },
    )

    return changes
