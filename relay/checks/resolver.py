"""Check resolution -- dice rolls, modifiers, pass/fail determination.

Invariant #8: LLM is never authoritative over mechanical state.
The LLM proposes checks; this module resolves them.

Improvements (step 10):
 - (#1)  Added sleight_of_hand (DEX) and animal_handling (WIS) to skill lists
 - (#4)  Extracted roll_d20() as shared public function (combat imports it)
 - (#5)  Stunned condition auto-fails skill checks
 - (#6)  Contested check support (resolve_contested_check)
 - (#7)  Passive check already-triggered filtering
 - (#8)  Charmed condition grants charmer advantage on social checks
 - (#9)  Natural 1/20 tracking on skill checks
 - (#11) MAX_CHECKS_PER_TURN guard on validate_checks (batch validator)
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

# (#1) Full D&D 5e skill list including sleight_of_hand and animal_handling
VALID_SKILLS = frozenset(
    {
        "athletics",
        "acrobatics",
        "sleight_of_hand",
        "stealth",
        "arcana",
        "history",
        "investigation",
        "nature",
        "religion",
        "animal_handling",
        "medicine",
        "perception",
        "insight",
        "survival",
        "intimidation",
        "persuasion",
        "deception",
        "performance",
    }
)

SKILL_ABILITY_MAP: dict[str, str] = {
    "athletics": "strength",
    "acrobatics": "dexterity",
    "sleight_of_hand": "dexterity",
    "stealth": "dexterity",
    "arcana": "intelligence",
    "history": "intelligence",
    "investigation": "intelligence",
    "nature": "intelligence",
    "religion": "intelligence",
    "animal_handling": "wisdom",
    "medicine": "wisdom",
    "perception": "wisdom",
    "insight": "wisdom",
    "survival": "wisdom",
    "intimidation": "charisma",
    "persuasion": "charisma",
    "deception": "charisma",
    "performance": "charisma",
}

# Social skills — charmed condition grants charmer advantage on these (#8)
SOCIAL_SKILLS = frozenset({"intimidation", "persuasion", "deception", "performance"})

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

# (#11) Maximum number of checks the LLM can propose per turn
MAX_CHECKS_PER_TURN = 3


def ability_modifier(score: int) -> int:
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """D&D-style proficiency bonus by level."""
    return (level - 1) // 4 + 2


# ---------------------------------------------------------------------------
# (#4) Shared d20 roll function — used by checks and combat
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


def validate_checks_batch(raw_checks: list[dict]) -> list[dict]:
    """Validate a batch of LLM-proposed checks, capping at MAX_CHECKS_PER_TURN (#11).

    Returns validated checks (at most MAX_CHECKS_PER_TURN). Logs a warning
    if the LLM proposed more than the allowed maximum.
    """
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
# (#5) Stunned auto-fail detection
# ---------------------------------------------------------------------------


def is_incapable_of_checks(conditions: list[dict] | None = None) -> bool:
    """Return True if active conditions prevent skill checks entirely.

    Stunned: cannot move or act, auto-fail STR and DEX saves.
    Incapacitated: cannot take actions.
    Both prevent voluntary skill checks.
    """
    for cond in conditions or []:
        cond_id = cond.get("condition_id", "")
        if cond_id in ("stunned", "incapacitated"):
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

    Args:
        charmer_checking: (#8) Set True when the checker is the charmer of the
            target — grants advantage on social skills.
        exhaustion_level: Character's exhaustion level (0-6).  Level 1+
            imposes disadvantage on ability checks per EXHAUSTION_EFFECTS.
    """
    adv_count = 0
    dis_count = 0

    if proposed_advantage:
        adv_count += 1
    if proposed_disadvantage:
        dis_count += 1

    # (#8) Charmed: charmer gets advantage on social checks against target
    if charmer_checking and skill in SOCIAL_SKILLS:
        adv_count += 1

    # Exhaustion level 1+: disadvantage on ability checks
    if exhaustion_level >= 1:
        dis_count += 1

    # Check active conditions for disadvantage
    for cond in conditions or []:
        cond_id = cond.get("condition_id", "")

        if cond_id in ("poisoned", "frightened") or (
            cond_id == "restrained" and skill in CONDITION_DISADVANTAGE.get("restrained", [])
        ):
            dis_count += 1

    # Check environmental effects
    for effect in environmental_effects or []:
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

    Returns a result dict with roll, modifier, total, dc, passed, roll_mode, dice,
    natural_20, natural_1.

    (#5) If the character is stunned or incapacitated, the check auto-fails.
    (#9) Includes natural_20 and natural_1 booleans for narrative enrichment.
    """
    skill = check["skill"]
    dc = check["dc"]

    # (#5) Stunned/incapacitated: auto-fail
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

    # (#9) Track natural extremes for narrative enrichment
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
# (#6) Contested checks (opposed skill contests)
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
    """Resolve a contested check between two characters (e.g., grapple, deception vs insight).

    Both sides roll their respective skills. Higher total wins.
    Ties go to the defender (status quo holds).

    Returns a result dict with both roll results and the winner.
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

    # Ties go to defender (status quo holds)
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
    Advantage applies +5.
    """
    governing_ability = SKILL_ABILITY_MAP.get(skill, "wisdom")
    score = ability_scores.get(governing_ability, 10)
    mod = ability_modifier(score)

    prof = proficiency_bonus(level) if skill in skill_proficiencies else 0
    base = 10 + mod + prof

    # Check conditions for disadvantage on this skill
    has_disadvantage = False
    for cond in conditions or []:
        cond_id = cond.get("condition_id", "")
        if cond_id == "poisoned" or cond_id.startswith("exhaustion"):
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
    already_triggered: list[str] | None = None,
) -> list[dict]:
    """Evaluate passive checks against hidden elements in the scene state.

    Hidden elements are stored in scene_state["hidden_elements"] as:
    [{"id": "...", "dc": int, "skill": "perception|insight|investigation",
      "hint": "text injected into narrator prompt on success"}]

    Args:
        already_triggered: (#7) List of element IDs that were already detected
            in previous turns. These are skipped to avoid repeated notifications.

    Returns a list of triggered hints (excluding already-triggered elements).
    """
    hidden = scene_state.get("hidden_elements", [])
    if not hidden:
        return []

    triggered_ids = set(already_triggered or [])

    triggered = []
    for element in hidden:
        element_id = element.get("id", "")

        # (#7) Skip elements already detected in previous turns
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
