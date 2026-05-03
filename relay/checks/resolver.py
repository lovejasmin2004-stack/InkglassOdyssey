"""Check resolution -- dice rolls, modifiers, pass/fail determination.

Invariant #8: LLM is never authoritative over mechanical state.
The LLM proposes checks; this module resolves them.
"""
from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

VALID_SKILLS = frozenset({
    "athletics", "acrobatics", "stealth",
    "arcana", "history", "investigation", "nature", "religion",
    "medicine", "perception", "insight",
    "intimidation", "persuasion", "deception", "performance",
    "survival",
})

SKILL_ABILITY_MAP: dict[str, str] = {
    "athletics": "strength",
    "acrobatics": "dexterity",
    "stealth": "dexterity",
    "arcana": "intelligence",
    "history": "intelligence",
    "investigation": "intelligence",
    "nature": "intelligence",
    "religion": "intelligence",
    "medicine": "wisdom",
    "perception": "wisdom",
    "insight": "wisdom",
    "intimidation": "charisma",
    "persuasion": "charisma",
    "deception": "charisma",
    "performance": "charisma",
    "survival": "wisdom",
}

# Conditions that grant disadvantage on specific check categories.
CONDITION_DISADVANTAGE: dict[str, list[str]] = {
    "poisoned": list(VALID_SKILLS),  # disadvantage on all ability checks
    "frightened": [],  # disadvantage on ability checks while source visible (handled by scene state)
    "exhaustion_1": list(VALID_SKILLS),  # exhaustion level 1+: disadvantage on checks
    "restrained": ["acrobatics", "athletics", "stealth"],
}

# Scene-state environmental effects that grant advantage/disadvantage.
ENVIRONMENT_ADVANTAGE: dict[str, dict[str, str]] = {
    "darkness": {"perception": "disadvantage"},
    "difficult_terrain": {"acrobatics": "disadvantage", "stealth": "disadvantage"},
    "high_ground": {},  # applies to ranged attacks, not skill checks
    "extreme_weather": {"perception": "disadvantage", "survival": "disadvantage"},
}

_DC_MIN = 5
_DC_MAX = 30


def ability_modifier(score: int) -> int:
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """D&D-style proficiency bonus by level."""
    return (level - 1) // 4 + 2


def validate_check(check: dict) -> dict:
    """Validate and clamp an LLM-proposed check. Never trust raw LLM values."""
    skill = check.get("skill", "").lower().strip()
    if skill not in VALID_SKILLS:
        logger.warning("Invalid skill proposed, defaulting to perception", extra={"proposed": skill})
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


def determine_roll_mode(
    *,
    proposed_advantage: bool,
    proposed_disadvantage: bool,
    conditions: list[dict] | None = None,
    environmental_effects: list[str] | None = None,
    skill: str,
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

    # Check active conditions for disadvantage
    for cond in (conditions or []):
        cond_id = cond.get("condition_id", "")

        if cond_id == "poisoned":
            dis_count += 1
        elif cond_id == "frightened":
            dis_count += 1
        elif cond_id.startswith("exhaustion"):
            dis_count += 1
        elif cond_id == "restrained" and skill in CONDITION_DISADVANTAGE.get("restrained", []):
            dis_count += 1

    # Check environmental effects
    for effect in (environmental_effects or []):
        env_rules = ENVIRONMENT_ADVANTAGE.get(effect, {})
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


def _roll_d20(mode: str) -> tuple[int, list[int]]:
    """Roll a d20 with the given mode. Returns (final_roll, all_dice)."""
    if mode == "advantage":
        dice = [random.randint(1, 20), random.randint(1, 20)]
        return max(dice), dice
    elif mode == "disadvantage":
        dice = [random.randint(1, 20), random.randint(1, 20)]
        return min(dice), dice
    else:
        roll = random.randint(1, 20)
        return roll, [roll]


def resolve_check(
    check: dict,
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
    *,
    conditions: list[dict] | None = None,
    environmental_effects: list[str] | None = None,
) -> dict:
    """Roll a d20 and resolve a single check against a character's stats.

    Returns a result dict with roll, modifier, total, dc, passed, roll_mode, dice.
    """
    skill = check["skill"]
    dc = check["dc"]

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
    )

    roll, dice = _roll_d20(roll_mode)
    total = roll + total_modifier
    passed = total >= dc

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
    Advantage applies +5.
    """
    governing_ability = SKILL_ABILITY_MAP.get(skill, "wisdom")
    score = ability_scores.get(governing_ability, 10)
    mod = ability_modifier(score)

    prof = proficiency_bonus(level) if skill in skill_proficiencies else 0
    base = 10 + mod + prof

    # Check conditions for disadvantage on this skill
    has_disadvantage = False
    for cond in (conditions or []):
        cond_id = cond.get("condition_id", "")
        if cond_id == "poisoned":
            has_disadvantage = True
        elif cond_id.startswith("exhaustion"):
            has_disadvantage = True

    if has_disadvantage:
        base -= 5

    return base


PASSIVE_CHECK_SKILLS = ("perception", "insight", "investigation")


def evaluate_passive_checks(
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
    scene_state: dict,
    *,
    conditions: list[dict] | None = None,
) -> list[dict]:
    """Evaluate passive checks against hidden elements in the scene state.

    Hidden elements are stored in scene_state["hidden_elements"] as:
    [{"id": "...", "dc": int, "skill": "perception|insight|investigation",
      "hint": "text injected into narrator prompt on success"}]

    Returns a list of triggered hints.
    """
    hidden = scene_state.get("hidden_elements", [])
    if not hidden:
        return []

    triggered = []
    for element in hidden:
        skill = element.get("skill", "perception")
        if skill not in PASSIVE_CHECK_SKILLS:
            skill = "perception"

        dc = element.get("dc", 15)
        passive_value = compute_passive_check(
            skill, ability_scores, skill_proficiencies, level,
            conditions=conditions,
        )

        if passive_value >= dc:
            triggered.append({
                "element_id": element.get("id", ""),
                "skill": skill,
                "dc": dc,
                "passive_value": passive_value,
                "hint": element.get("hint", ""),
            })
            logger.info(
                "Passive check triggered",
                extra={
                    "element_id": element.get("id", ""),
                    "skill": skill,
                    "dc": dc,
                    "passive_value": passive_value,
                },
            )

    return triggered
