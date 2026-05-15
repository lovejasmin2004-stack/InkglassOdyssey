"""Check resolution -- dice rolls, modifiers, pass/fail determination.

Invariant #8: LLM is never authoritative over mechanical state.
The LLM proposes checks; this module resolves them.

Canonical skill, ability, and condition definitions live in relay.registry.
"""

from __future__ import annotations

import logging
import random

from relay.registry import (
    CONDITIONS,
    ENVIRONMENT_RULES,
    EXHAUSTION_MAX,
    PASSIVE_SKILLS,
    SKILL_ABILITY,
    SKILLS,
    SOCIAL_SKILLS,
    exhaustion_def,
)

logger = logging.getLogger(__name__)

VALID_SKILLS = SKILLS
SKILL_ABILITY_MAP = SKILL_ABILITY
PASSIVE_CHECK_SKILLS = PASSIVE_SKILLS

_DC_MIN = 5
_DC_MAX = 30

MAX_CHECKS_PER_TURN = 3


def ability_modifier(score: int) -> int:
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """D&D-style proficiency bonus by level."""
    return (level - 1) // 4 + 2


# ---------------------------------------------------------------------------
# Shared d20 roll function — used by checks and combat
# ---------------------------------------------------------------------------


def roll_d20(mode: str) -> tuple[int, list[int]]:
    """Roll a d20 with the given mode. Returns (final_roll, all_dice).

    Modes: "advantage" (2d20 take highest), "disadvantage" (2d20 take lowest),
    "straight" (1d20).

    This is the single source of truth for d20 rolling across the codebase.
    Combat resolver imports this rather than implementing its own.
    """
    if mode == "advantage":
        dice = [random.randint(1, 20), random.randint(1, 20)]
        return max(dice), dice
    elif mode == "disadvantage":
        dice = [random.randint(1, 20), random.randint(1, 20)]
        return min(dice), dice
    else:
        roll = random.randint(1, 20)
        return roll, [roll]


def validate_check(check: dict) -> dict:
    """Validate and clamp an LLM-proposed check. Never trust raw LLM values."""
    skill = check.get("skill", "").lower().strip()
    if skill not in VALID_SKILLS:
        logger.warning(
            "Invalid skill proposed, defaulting to perception",
            extra={"proposed": skill},
        )
        skill = "perception"

    dc = check.get("dc", 15)
    if not isinstance(dc, int):
        dc = 15
    dc = max(_DC_MIN, min(_DC_MAX, dc))

    advantage = check.get("advantage", False)
    disadvantage = check.get("disadvantage", False)
    if not isinstance(advantage, bool):
        advantage = False
    if not isinstance(disadvantage, bool):
        disadvantage = False

    return {
        "skill": skill,
        "dc": dc,
        "reason": check.get("reason", ""),
        "advantage": advantage,
        "disadvantage": disadvantage,
    }


def validate_checks_batch(raw_checks: list[dict]) -> list[dict]:
    """Validate a batch of LLM-proposed checks, capping at MAX_CHECKS_PER_TURN."""
    if len(raw_checks) > MAX_CHECKS_PER_TURN:
        logger.warning(
            "LLM proposed too many checks, capping",
            extra={
                "proposed_count": len(raw_checks),
                "max_allowed": MAX_CHECKS_PER_TURN,
            },
        )
        raw_checks = raw_checks[:MAX_CHECKS_PER_TURN]

    return [validate_check(c) for c in raw_checks]


# ---------------------------------------------------------------------------
# Registry-driven condition disadvantage
# ---------------------------------------------------------------------------


def _condition_disadvantage_on_skill(condition_id: str, skill: str) -> bool:
    """True if a condition imposes disadvantage on this skill check.

    Looks up the canonical registry; handles graduated exhaustion.
    """
    if condition_id.startswith("exhaustion_"):
        try:
            level = int(condition_id.removeprefix("exhaustion_"))
        except ValueError:
            return False
        return exhaustion_def(level).disadvantage_on_all_checks

    cdef = CONDITIONS.get(condition_id)
    if cdef is None:
        return False
    if cdef.disadvantage_on_all_checks:
        return True
    return skill in cdef.disadvantage_on_skills


def is_incapable_of_checks(conditions: list[dict] | None = None) -> bool:
    """Return True if active conditions prevent skill checks entirely.

    Stunned/incapacitated: cannot take actions, auto-fail voluntary checks.
    """
    for cond in conditions or []:
        cond_id = cond.get("condition_id", "")
        cdef = CONDITIONS.get(cond_id)
        if cdef and cdef.incapacitated:
            return True
    return False


def determine_roll_mode(
    *,
    proposed_advantage: bool,
    proposed_disadvantage: bool,
    conditions: list[dict] | None = None,
    environmental_effects: list[str] | None = None,
    skill: str,
    charmer_checking: bool = False,
    exhaustion_level: int = 0,
) -> str:
    """Determine final roll mode from all sources.

    Returns "advantage", "disadvantage", or "straight".
    Any number of advantage and disadvantage sources cancel to straight.
    """
    adv_count = 0
    dis_count = 0

    if proposed_advantage:
        adv_count += 1
    if proposed_disadvantage:
        dis_count += 1

    if charmer_checking and skill in SOCIAL_SKILLS:
        adv_count += 1

    if exhaustion_level >= 1:
        dis_count += 1

    for cond in conditions or []:
        cond_id = cond.get("condition_id", "")
        if _condition_disadvantage_on_skill(cond_id, skill):
            dis_count += 1

    for effect in environmental_effects or []:
        env_rules = ENVIRONMENT_RULES.get(effect, {})
        if skill in env_rules:
            if env_rules[skill] == "advantage":
                adv_count += 1
            elif env_rules[skill] == "disadvantage":
                dis_count += 1

    if adv_count > 0 and dis_count > 0:
        return "straight"
    if adv_count > 0:
        return "advantage"
    if dis_count > 0:
        return "disadvantage"
    return "straight"


def resolve_check(
    check: dict,
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
    *,
    conditions: list[dict] | None = None,
    environmental_effects: list[str] | None = None,
    charmer_checking: bool = False,
    exhaustion_level: int = 0,
) -> dict:
    """Roll a d20 and resolve a single check against a character's stats.

    Returns a result dict with roll, modifier, total, dc, passed, roll_mode,
    dice, natural_20, natural_1.

    If the character is stunned or incapacitated, the check auto-fails.
    """
    skill = check["skill"]
    dc = check["dc"]

    if is_incapable_of_checks(conditions):
        logger.info(
            "Check auto-failed due to incapacitating condition",
            extra={"skill": skill, "dc": dc},
        )
        return {
            "skill": skill,
            "dc": dc,
            "reason": check.get("reason", ""),
            "roll": 0,
            "dice": [0],
            "roll_mode": "auto_fail",
            "modifier": 0,
            "total": 0,
            "passed": False,
            "natural_20": False,
            "natural_1": False,
            "auto_fail_reason": "incapacitated",
        }

    governing_ability = SKILL_ABILITY_MAP.get(skill, "wisdom")
    score = ability_scores.get(governing_ability, 10)
    mod = ability_modifier(score)

    prof = proficiency_bonus(level) if skill in skill_proficiencies else 0
    total_modifier = mod + prof

    roll_mode = determine_roll_mode(
        proposed_advantage=check.get("advantage", False),
        proposed_disadvantage=check.get("disadvantage", False),
        conditions=conditions,
        environmental_effects=environmental_effects,
        skill=skill,
        charmer_checking=charmer_checking,
        exhaustion_level=exhaustion_level,
    )

    roll, dice = roll_d20(roll_mode)
    total = roll + total_modifier
    passed = total >= dc

    natural_20 = roll == 20
    natural_1 = roll == 1

    result = {
        "skill": skill,
        "dc": dc,
        "reason": check.get("reason", ""),
        "roll": roll,
        "dice": dice,
        "roll_mode": roll_mode,
        "modifier": total_modifier,
        "total": total,
        "passed": passed,
        "natural_20": natural_20,
        "natural_1": natural_1,
    }

    logger.info(
        "Check resolved",
        extra={
            "skill": skill,
            "dc": dc,
            "roll": roll,
            "dice": dice,
            "roll_mode": roll_mode,
            "modifier": total_modifier,
            "total": total,
            "passed": passed,
            "natural_20": natural_20,
            "natural_1": natural_1,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Contested checks (opposed skill contests)
# ---------------------------------------------------------------------------


def resolve_contested_check(
    attacker_check: dict,
    attacker_ability_scores: dict[str, int],
    attacker_skill_proficiencies: list[str],
    attacker_level: int,
    defender_check: dict,
    defender_ability_scores: dict[str, int],
    defender_skill_proficiencies: list[str],
    defender_level: int,
    *,
    attacker_conditions: list[dict] | None = None,
    defender_conditions: list[dict] | None = None,
    attacker_environmental_effects: list[str] | None = None,
    defender_environmental_effects: list[str] | None = None,
) -> dict:
    """Resolve a contested check between two characters.

    Both sides roll their respective skills. Higher total wins.
    Ties go to the defender (status quo holds).
    """
    attacker_result = resolve_check(
        attacker_check,
        attacker_ability_scores,
        attacker_skill_proficiencies,
        attacker_level,
        conditions=attacker_conditions,
        environmental_effects=attacker_environmental_effects,
    )

    defender_result = resolve_check(
        defender_check,
        defender_ability_scores,
        defender_skill_proficiencies,
        defender_level,
        conditions=defender_conditions,
        environmental_effects=defender_environmental_effects,
    )

    attacker_wins = attacker_result["total"] > defender_result["total"]

    result = {
        "attacker": attacker_result,
        "defender": defender_result,
        "winner": "attacker" if attacker_wins else "defender",
        "attacker_total": attacker_result["total"],
        "defender_total": defender_result["total"],
        "tie": attacker_result["total"] == defender_result["total"],
    }

    logger.info(
        "Contested check resolved",
        extra={
            "attacker_skill": attacker_check.get("skill"),
            "defender_skill": defender_check.get("skill"),
            "attacker_total": attacker_result["total"],
            "defender_total": defender_result["total"],
            "winner": result["winner"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# Passive checks (Invariant #22)
# ---------------------------------------------------------------------------


def compute_passive_check(
    skill: str,
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
    *,
    conditions: list[dict] | None = None,
) -> int:
    """Compute a passive check value: 10 + ability modifier + proficiency bonus.

    Disadvantage from conditions applies a -5 penalty (D&D 5e rule).
    """
    governing_ability = SKILL_ABILITY_MAP.get(skill, "wisdom")
    score = ability_scores.get(governing_ability, 10)
    mod = ability_modifier(score)

    prof = proficiency_bonus(level) if skill in skill_proficiencies else 0
    base = 10 + mod + prof

    has_disadvantage = any(
        _condition_disadvantage_on_skill(cond.get("condition_id", ""), skill)
        for cond in (conditions or [])
    )
    if has_disadvantage:
        base -= 5

    return base


def evaluate_passive_checks(
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
    scene_state: dict,
    *,
    conditions: list[dict] | None = None,
    already_triggered: list[str] | None = None,
) -> list[dict]:
    """Evaluate passive checks against hidden elements in the scene state.

    Hidden elements are stored in scene_state["hidden_elements"] as:
    [{"id": "...", "dc": int, "skill": "perception|insight|investigation",
      "hint": "text injected into narrator prompt on success"}]

    Args:
        already_triggered: List of element IDs already detected in previous
            turns. These are skipped to avoid repeated notifications.

    Returns a list of triggered hints (excluding already-triggered elements).
    """
    hidden = scene_state.get("hidden_elements", [])
    if not hidden:
        return []

    triggered_ids = set(already_triggered or [])

    triggered = []
    for element in hidden:
        element_id = element.get("id", "")

        if element_id in triggered_ids:
            continue

        skill = element.get("skill", "perception")
        if skill not in PASSIVE_CHECK_SKILLS:
            skill = "perception"

        dc = element.get("dc", 15)
        passive_value = compute_passive_check(
            skill,
            ability_scores,
            skill_proficiencies,
            level,
            conditions=conditions,
        )

        if passive_value >= dc:
            triggered.append(
                {
                    "element_id": element_id,
                    "skill": skill,
                    "dc": dc,
                    "passive_value": passive_value,
                    "hint": element.get("hint", ""),
                }
            )
            logger.info(
                "Passive check triggered",
                extra={
                    "element_id": element_id,
                    "skill": skill,
                    "dc": dc,
                    "passive_value": passive_value,
                },
            )

    return triggered
