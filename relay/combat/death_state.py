"""Narrative death state system.

At 0 HP: character enters compromised state. Per-world terminology varies
(Broken, Incapacitated, Critically Wounded) but mechanics are identical:
- Disadvantage on all checks
- Speed halved
- Gain 1 exhaustion per turn at 0 HP (max 3 from this source)
- Any healing above 0 HP ends death state (exhaustion remains)
- Exhaustion 6 = permanent retirement

This is a ticking clock, not a coin flip.
"""

from __future__ import annotations

import logging

from relay.combat.conditions import EXHAUSTION_MAX, increment_exhaustion

logger = logging.getLogger(__name__)

DEATH_STATE_EXHAUSTION_CAP = 3


def enter_death_state(hp_current: int, exhaustion_level: int) -> dict:
    """Check if a character should enter death state and apply initial effects."""
    if hp_current > 0:
        return {"in_death_state": False, "exhaustion_level": exhaustion_level}

    new_exhaustion = increment_exhaustion(exhaustion_level)

    logger.info(
        "Character entered death state",
        extra={"exhaustion_level": new_exhaustion},
    )

    return {
        "in_death_state": True,
        "exhaustion_level": new_exhaustion,
        "death_state_exhaustion_gained": 1,
    }


def tick_death_state(
    hp_current: int,
    exhaustion_level: int,
    death_state_exhaustion_gained: int,
) -> dict:
    """Called each turn while at 0 HP. Gains exhaustion up to the cap from this source."""
    if hp_current > 0:
        return {
            "in_death_state": False,
            "exhaustion_level": exhaustion_level,
            "death_state_exhaustion_gained": death_state_exhaustion_gained,
        }

    if death_state_exhaustion_gained >= DEATH_STATE_EXHAUSTION_CAP:
        return {
            "in_death_state": True,
            "exhaustion_level": exhaustion_level,
            "death_state_exhaustion_gained": death_state_exhaustion_gained,
            "retired": exhaustion_level >= EXHAUSTION_MAX,
        }

    new_exhaustion = increment_exhaustion(exhaustion_level)
    new_gained = death_state_exhaustion_gained + 1

    logger.info(
        "Death state tick",
        extra={
            "exhaustion_level": new_exhaustion,
            "death_state_exhaustion_gained": new_gained,
        },
    )

    return {
        "in_death_state": True,
        "exhaustion_level": new_exhaustion,
        "death_state_exhaustion_gained": new_gained,
        "retired": new_exhaustion >= EXHAUSTION_MAX,
    }


def heal_from_death_state(
    hp_current: int,
    healing: int,
    exhaustion_level: int,
) -> dict:
    """Apply healing to a character in death state.

    Any healing above 0 HP ends death state. Exhaustion remains (recovery arc).
    """
    new_hp = hp_current + healing

    if new_hp > 0:
        logger.info(
            "Character healed out of death state",
            extra={"new_hp": new_hp, "exhaustion_remains": exhaustion_level},
        )
        return {
            "hp_current": new_hp,
            "in_death_state": False,
            "exhaustion_level": exhaustion_level,
        }

    return {
        "hp_current": new_hp,
        "in_death_state": True,
        "exhaustion_level": exhaustion_level,
    }


def is_retired(exhaustion_level: int) -> bool:
    """At exhaustion 6 the character is permanently retired."""
    return exhaustion_level >= EXHAUSTION_MAX
