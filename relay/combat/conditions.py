"""Active-effects model for character conditions.

A condition instance is a dict with the shape defined by
relay.schemas.ConditionEntry. These helpers are pure: they take a list of
condition dicts and return a new list. No DB writes here -- callers persist
the result back to Character.conditions (or future Combatant.conditions).

Pattern adopted from Foundry dnd5e's ActiveEffect5e:
    * Duration tracked in rounds/turns/minutes, ticked on combat lifecycle events
    * Rider conditions auto-applied when a parent condition is added
    * Suppression: rider conditions removed when their parent is removed

Invariant #22: Conditions have duration tracking.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from relay.registry import (
    CONDITIONS,
    EXHAUSTION_MAX,
    ConditionDef,
    DurationUnit,
    SourceType,
    exhaustion_def,
)

logger = logging.getLogger(__name__)

ConditionInstance = dict[str, Any]
"""Runtime shape: {instance_id, condition_id, duration_remaining, duration_unit,
   rider_of, source, source_type}. Validated by schemas.ConditionEntry."""


# ---------------------------------------------------------------------------
# Registry lookup
# ---------------------------------------------------------------------------


def _condition_def(condition_id: str) -> ConditionDef | None:
    """Look up the definition for a condition_id, including graduated exhaustion."""
    if condition_id.startswith("exhaustion_"):
        try:
            level = int(condition_id.removeprefix("exhaustion_"))
        except ValueError:
            return None
        return exhaustion_def(level)
    return CONDITIONS.get(condition_id)


# ---------------------------------------------------------------------------
# Instance lifecycle — pure functions, return new lists
# ---------------------------------------------------------------------------


def _new_instance(
    condition_id: str,
    *,
    duration_remaining: int | None,
    duration_unit: DurationUnit,
    source: str,
    source_type: SourceType,
    rider_of: str | None,
) -> ConditionInstance:
    return {
        "instance_id": str(uuid.uuid4()),
        "condition_id": condition_id,
        "duration_remaining": duration_remaining,
        "duration_unit": duration_unit,
        "source": source,
        "source_type": source_type,
        "rider_of": rider_of,
    }


def add_condition(
    conditions: list[ConditionInstance],
    condition_id: str,
    *,
    source: str,
    source_type: SourceType = "other",
    duration_remaining: int | None = None,
    duration_unit: DurationUnit = "turns",
) -> list[ConditionInstance]:
    """Return a new list with the condition (and any rider conditions) added.

    If the condition is unknown, the call is a no-op and a warning is logged.
    """
    cdef = _condition_def(condition_id)
    if cdef is None:
        logger.warning("Unknown condition_id", extra={"condition_id": condition_id})
        return list(conditions)

    parent = _new_instance(
        condition_id,
        duration_remaining=duration_remaining,
        duration_unit=duration_unit,
        source=source,
        source_type=source_type,
        rider_of=None,
    )
    result = [*list(conditions), parent]

    for rider_id in cdef.rider_conditions:
        result.append(
            _new_instance(
                rider_id,
                duration_remaining=duration_remaining,
                duration_unit=duration_unit,
                source=source,
                source_type=source_type,
                rider_of=parent["instance_id"],
            )
        )

    return result


def remove_condition(
    conditions: list[ConditionInstance],
    condition_id_positional: str | None = None,
    *,
    instance_id: str | None = None,
    condition_id: str | None = None,
) -> list[ConditionInstance]:
    """Remove a condition by instance_id or condition_id.

    Supports both calling conventions:
      remove_condition(conditions, "poisoned")          # positional
      remove_condition(conditions, condition_id="...")   # keyword
      remove_condition(conditions, instance_id="...")    # by unique instance

    When removing by condition_id, all matching parent instances and their
    riders are removed. When removing by instance_id, only that instance
    and its riders.
    """
    cid = condition_id_positional or condition_id
    if instance_id is None and cid is None:
        return list(conditions)

    if instance_id is not None:
        return [c for c in conditions if c.get("instance_id") != instance_id and c.get("rider_of") != instance_id]

    removed_instance_ids: set[str] = set()
    result: list[ConditionInstance] = []
    for c in conditions:
        if c.get("condition_id") == cid and c.get("rider_of") is None:
            iid = c.get("instance_id")
            if iid:
                removed_instance_ids.add(iid)
            continue
        result.append(c)

    if removed_instance_ids:
        result = [c for c in result if c.get("rider_of") not in removed_instance_ids]

    return result


def tick_durations(
    conditions: list[ConditionInstance],
    *,
    unit: DurationUnit,
) -> list[ConditionInstance]:
    """Decrement duration_remaining for conditions whose duration_unit matches.

    Conditions reaching 0 are removed, along with their riders. Conditions with
    duration_remaining=None or duration_unit="permanent"/"until_long_rest" are
    untouched by per-turn/per-round ticks.
    """
    expired_instance_ids: set[str] = set()
    next_state: list[ConditionInstance] = []

    for cond in conditions:
        if cond.get("rider_of") is not None:
            next_state.append(dict(cond))
            continue
        if cond.get("duration_unit") != unit:
            next_state.append(dict(cond))
            continue
        remaining = cond.get("duration_remaining")
        if remaining is None:
            next_state.append(dict(cond))
            continue
        new_remaining = remaining - 1
        if new_remaining <= 0:
            expired_instance_ids.add(cond["instance_id"])
            continue
        updated = dict(cond)
        updated["duration_remaining"] = new_remaining
        next_state.append(updated)

    if not expired_instance_ids:
        return next_state

    return [c for c in next_state if c.get("rider_of") not in expired_instance_ids]


# ---------------------------------------------------------------------------
# Legacy API — backward-compatible wrappers for existing endpoints/tests
# ---------------------------------------------------------------------------


def apply_condition(
    conditions: list[ConditionInstance],
    condition_id: str,
    duration_turns: int | None = None,
    expiry_turn: int | None = None,
    source: str = "",
) -> list[ConditionInstance]:
    """Add a condition with legacy duration_turns/expiry_turn arguments.

    Wraps add_condition, preserving old-style fields for callers that inspect
    duration_turns or expiry_turn directly.
    """
    if duration_turns is None and expiry_turn is None:
        logger.warning(
            "Condition must have duration or expiry",
            extra={"condition_id": condition_id},
        )
        return list(conditions)

    result = add_condition(
        conditions,
        condition_id,
        source=source,
        duration_remaining=duration_turns,
        duration_unit="turns",
    )

    if len(result) > len(conditions):
        parent = result[len(conditions)]
        if duration_turns is not None:
            parent["duration_turns"] = duration_turns
        if expiry_turn is not None:
            parent["expiry_turn"] = expiry_turn

    return result


def tick_conditions(
    conditions: list[ConditionInstance],
    current_turn: int,
) -> list[ConditionInstance]:
    """Advance conditions by one turn using legacy duration_turns/expiry_turn.

    Handles both old-style fields (duration_turns, expiry_turn) and new-style
    (duration_remaining + duration_unit).
    """
    remaining: list[ConditionInstance] = []
    for cond in conditions:
        if cond.get("expiry_turn") is not None and current_turn >= cond["expiry_turn"]:
            logger.info(
                "Condition expired",
                extra={"condition_id": cond.get("condition_id")},
            )
            continue

        updated = dict(cond)
        dt = updated.get("duration_turns")
        if dt is not None:
            dt -= 1
            if dt <= 0:
                logger.info(
                    "Condition duration ended",
                    extra={"condition_id": cond.get("condition_id")},
                )
                continue
            updated["duration_turns"] = dt
            updated["duration_remaining"] = dt

        remaining.append(updated)
    return remaining


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def has_condition(conditions: list[ConditionInstance], condition_id: str) -> bool:
    """True if any active instance has this condition_id (parent or rider)."""
    return any(c.get("condition_id") == condition_id for c in conditions)


def is_incapacitated(conditions: list[ConditionInstance]) -> bool:
    """True if any active condition imposes the incapacitated state."""
    for cond in conditions:
        cdef = _condition_def(cond.get("condition_id", ""))
        if cdef and cdef.incapacitated:
            return True
    return False


# ---------------------------------------------------------------------------
# Exhaustion helpers
# ---------------------------------------------------------------------------


def increment_exhaustion(current_level: int) -> int:
    """Increment exhaustion by 1, capped at EXHAUSTION_MAX."""
    return min(current_level + 1, EXHAUSTION_MAX)


def reduce_exhaustion(current_level: int, amount: int = 1) -> int:
    """Reduce exhaustion (e.g., after long rest). Minimum 0."""
    return max(0, current_level - amount)


# ---------------------------------------------------------------------------
# Combat modifier queries — registry-driven
# ---------------------------------------------------------------------------


def get_attack_modifiers(
    conditions: list[ConditionInstance],
    exhaustion_level: int,
    *,
    source_visible: bool = True,
) -> dict:
    """Determine advantage/disadvantage on the character's own attacks."""
    has_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        cdef = _condition_def(cid)
        if cdef is None:
            continue
        if cdef.disadvantage_on_attacks:
            if cdef.requires_source_visible and not source_visible:
                continue
            has_disadvantage = True

    edef = exhaustion_def(exhaustion_level) if exhaustion_level >= 1 else None
    if edef and edef.disadvantage_on_attacks:
        has_disadvantage = True

    return {"attack_disadvantage": has_disadvantage}


def get_defense_modifiers(
    conditions: list[ConditionInstance],
    *,
    attack_range: str = "melee",
) -> dict:
    """Determine if attackers get advantage/disadvantage against this target.

    Conditions with ``range_dependent_advantage`` grant advantage only at the
    specified range, and disadvantage at all other ranges.
    """
    attackers_have_advantage = False
    attackers_have_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        cdef = _condition_def(cid)
        if cdef is None:
            continue
        if cdef.grants_advantage_to_attackers:
            if cdef.range_dependent_advantage is not None:
                if cdef.range_dependent_advantage == "melee_only" and attack_range == "melee":
                    attackers_have_advantage = True
                elif cdef.range_dependent_advantage == "ranged_only" and attack_range == "ranged":
                    attackers_have_advantage = True
                else:
                    attackers_have_disadvantage = True
            else:
                attackers_have_advantage = True

    return {
        "attackers_have_advantage": attackers_have_advantage,
        "attackers_have_disadvantage": attackers_have_disadvantage,
    }


def get_save_modifiers(
    conditions: list[ConditionInstance],
    exhaustion_level: int,
    save_type: str,
) -> dict:
    """Determine advantage/disadvantage and auto-fail on saving throws."""
    auto_fail = False
    has_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        cdef = _condition_def(cid)
        if cdef is None:
            continue
        if cdef.auto_fail_strength_saves and save_type == "strength":
            auto_fail = True
        if cdef.auto_fail_dexterity_saves and save_type == "dexterity":
            auto_fail = True
        if save_type in cdef.disadvantage_on_saves:
            has_disadvantage = True

    edef = exhaustion_def(exhaustion_level) if exhaustion_level >= 1 else None
    if edef and save_type in edef.disadvantage_on_saves:
        has_disadvantage = True

    return {"auto_fail": auto_fail, "save_disadvantage": has_disadvantage}
