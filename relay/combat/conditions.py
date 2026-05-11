"""Condition tracking and mechanical effects.

Conditions are stored on character/NPC as an array of:
  {condition_id, duration_turns or expiry_turn, source}

Every condition must have a defined end — no open-ended conditions.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VALID_CONDITIONS = frozenset(
    {
        "poisoned",
        "frightened",
        "charmed",
        "stunned",
        "restrained",
        "incapacitated",
        "blinded",  # (#7) attacks have disadvantage, attacks against have advantage
        "prone",  # (#7) melee attacks against have advantage, ranged have disadvantage
    }
)

EXHAUSTION_MAX = 6

CONDITION_EFFECTS: dict[str, dict] = {
    "poisoned": {
        "attack_disadvantage": True,
        "check_disadvantage": True,
    },
    "frightened": {
        "check_disadvantage_while_source_visible": True,
        "cannot_move_toward_source": True,
    },
    "charmed": {
        "cannot_attack_charmer": True,
        "charmer_social_advantage": True,
    },
    "stunned": {
        "cannot_act": True,
        "auto_fail_str_saves": True,
        "auto_fail_dex_saves": True,
    },
    "restrained": {
        "speed_zero": True,
        "dex_save_disadvantage": True,
        "attacks_against_advantage": True,
    },
    "incapacitated": {
        "cannot_act": True,
        "attacks_against_advantage": True,
    },
    "blinded": {
        "attack_disadvantage": True,
        "attacks_against_advantage": True,
    },
    "prone": {
        "melee_attacks_against_advantage": True,
        "ranged_attacks_against_disadvantage": True,
        "attack_disadvantage": True,  # own attacks at disadvantage
        "movement_cost_doubled": True,
    },
}

EXHAUSTION_EFFECTS: dict[int, dict] = {
    1: {"check_disadvantage": True},
    2: {"speed_halved": True},
    3: {"attack_disadvantage": True, "save_disadvantage": True},
    4: {"hp_max_halved": True},
    5: {"speed_zero": True},
    6: {"character_retired": True},
}


def apply_condition(
    conditions: list[dict],
    condition_id: str,
    duration_turns: int | None = None,
    expiry_turn: int | None = None,
    source: str = "",
) -> list[dict]:
    """Add a condition to the conditions list. Returns the updated list."""
    if condition_id not in VALID_CONDITIONS:
        logger.warning("Invalid condition_id", extra={"condition_id": condition_id})
        return conditions

    if duration_turns is None and expiry_turn is None:
        logger.warning("Condition must have duration or expiry", extra={"condition_id": condition_id})
        return conditions

    entry = {"condition_id": condition_id, "source": source}
    if duration_turns is not None:
        entry["duration_turns"] = duration_turns
    if expiry_turn is not None:
        entry["expiry_turn"] = expiry_turn

    conditions.append(entry)
    logger.info("Condition applied", extra={"condition_id": condition_id, "source": source})
    return conditions


def remove_condition(conditions: list[dict], condition_id: str) -> list[dict]:
    """Remove all instances of a condition. Returns the updated list."""
    return [c for c in conditions if c.get("condition_id") != condition_id]


def tick_conditions(conditions: list[dict], current_turn: int) -> list[dict]:
    """Advance conditions by one turn. Remove expired ones."""
    remaining = []
    for cond in conditions:
        if cond.get("expiry_turn") is not None and current_turn >= cond["expiry_turn"]:
            logger.info("Condition expired", extra={"condition_id": cond["condition_id"]})
            continue
        if cond.get("duration_turns") is not None:
            cond["duration_turns"] -= 1
            if cond["duration_turns"] <= 0:
                logger.info("Condition duration ended", extra={"condition_id": cond["condition_id"]})
                continue
        remaining.append(cond)
    return remaining


def increment_exhaustion(current_level: int) -> int:
    """Increment exhaustion by 1, capped at EXHAUSTION_MAX."""
    return min(current_level + 1, EXHAUSTION_MAX)


def reduce_exhaustion(current_level: int, amount: int = 1) -> int:
    """Reduce exhaustion (e.g., after long rest). Minimum 0."""
    return max(0, current_level - amount)


def get_attack_modifiers(
    conditions: list[dict],
    exhaustion_level: int,
    *,
    source_visible: bool = True,
) -> dict:
    """Determine advantage/disadvantage on attacks from active conditions.

    (#4) Frightened: disadvantage on attacks while source is visible.
    (#7) Blinded/prone: disadvantage on own attacks.
    """
    has_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        if cid in ("poisoned", "restrained", "blinded", "prone") or (cid == "frightened" and source_visible):
            has_disadvantage = True

    if exhaustion_level >= 3:
        has_disadvantage = True

    return {"attack_disadvantage": has_disadvantage}


def get_defense_modifiers(
    conditions: list[dict],
    *,
    attack_range: str = "melee",
) -> dict:
    """Determine if attackers get advantage/disadvantage against this target.

    (#7) Blinded: attackers always have advantage.
    (#7) Prone: melee attackers have advantage, ranged attackers have disadvantage.

    Args:
        attack_range: "melee" or "ranged". Affects prone modifier.
    """
    attackers_have_advantage = False
    attackers_have_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        if cid in ("stunned", "restrained", "incapacitated", "blinded"):
            attackers_have_advantage = True
        elif cid == "prone":
            if attack_range == "melee":
                attackers_have_advantage = True
            else:
                attackers_have_disadvantage = True

    return {
        "attackers_have_advantage": attackers_have_advantage,
        "attackers_have_disadvantage": attackers_have_disadvantage,
    }


def get_save_modifiers(conditions: list[dict], exhaustion_level: int, save_type: str) -> dict:
    """Determine advantage/disadvantage and auto-fail on saving throws."""
    auto_fail = False
    has_disadvantage = False

    for cond in conditions:
        cid = cond.get("condition_id", "")
        if cid == "stunned" and save_type in ("strength", "dexterity"):
            auto_fail = True
        elif cid == "restrained" and save_type == "dexterity":
            has_disadvantage = True

    if exhaustion_level >= 3:
        has_disadvantage = True

    return {"auto_fail": auto_fail, "save_disadvantage": has_disadvantage}
