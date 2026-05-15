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
    ConditionDef,
    DurationUnit,
    SourceType,
    exhaustion_def,
)

logger = logging.getLogger(__name__)

ConditionInstance = dict[str, Any]
"""Runtime shape: {instance_id, condition_id, duration_remaining, duration_unit,
   rider_of, source_id, source_type}. Validated by schemas.ConditionEntry."""


def _condition_def(condition_id: str) -> ConditionDef | None:
    """Look up the definition for a condition_id, including graduated exhaustion."""
    if condition_id.startswith("exhaustion_"):
        try:
            level = int(condition_id.removeprefix("exhaustion_"))
        except ValueError:
            return None
        return exhaustion_def(level)
    return CONDITIONS.get(condition_id)


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
    result = list(conditions) + [parent]

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
    *,
    instance_id: str | None = None,
    condition_id: str | None = None,
) -> list[ConditionInstance]:
    """Remove a condition (and any of its riders) by instance_id or condition_id.

    instance_id is preferred (unique). condition_id removes the first match and
    is appropriate when callers don't track instance IDs (e.g., "cure poison").
    """
    if instance_id is None and condition_id is None:
        return list(conditions)

    target_instance_id: str | None = instance_id
    if target_instance_id is None:
        for cond in conditions:
            if (
                cond.get("condition_id") == condition_id
                and cond.get("rider_of") is None
            ):
                target_instance_id = cond["instance_id"]
                break
        if target_instance_id is None:
            return list(conditions)

    return [
        c
        for c in conditions
        if c["instance_id"] != target_instance_id
        and c.get("rider_of") != target_instance_id
    ]


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
