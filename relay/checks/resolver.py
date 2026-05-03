"""Check resolution — dice rolls, modifiers, pass/fail determination.

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

# Maps skills to their governing canonical ability score.
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

    return {
        "skill": skill,
        "dc": dc,
        "reason": check.get("reason", ""),
    }


def resolve_check(
    check: dict,
    ability_scores: dict[str, int],
    skill_proficiencies: list[str],
    level: int,
) -> dict:
    """Roll a d20 and resolve a single check against a character's stats.

    Returns a result dict with roll, modifier, total, dc, passed.
    """
    skill = check["skill"]
    dc = check["dc"]

    governing_ability = SKILL_ABILITY_MAP.get(skill, "wisdom")
    score = ability_scores.get(governing_ability, 10)
    mod = ability_modifier(score)

    prof = proficiency_bonus(level) if skill in skill_proficiencies else 0
    total_modifier = mod + prof

    roll = random.randint(1, 20)
    total = roll + total_modifier
    passed = total >= dc

    result = {
        "skill": skill,
        "dc": dc,
        "reason": check.get("reason", ""),
        "roll": roll,
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
            "modifier": total_modifier,
            "total": total,
            "passed": passed,
        },
    )
    return result
