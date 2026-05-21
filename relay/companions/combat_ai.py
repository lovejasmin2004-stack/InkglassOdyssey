"""Companion combat AI — automatic action resolution per turn.

Each companion gets one action per combat turn, chosen by behavior_type:
  aggressive  → attack the player's current target
  supportive  → heal the lowest-HP ally
  defensive   → protect action (interpose, draw aggression)

Note: This module is intentionally stateless — it resolves dice rolls and
returns an action result dict.  The combat system (relay/combat/) is
responsible for applying the results (damage, healing) to game state.

Design doc: docs/companion system.pdf §Combat Profile
"""

from __future__ import annotations

import logging

from relay.checks.resolver import ability_modifier, proficiency_bonus, roll_d20
from relay.combat.damage import parse_formula
from relay.combat.resolver import roll_dice

logger = logging.getLogger(__name__)


def resolve_companion_action(
    *,
    companion: dict,
    companion_data: dict,
    target: dict | None = None,
    allies: list[dict] | None = None,
) -> dict:
    """Resolve one automatic combat action for a companion.

    Parameters
    ----------
    companion : dict
        The companion state from character.companions.
    companion_data : dict
        The NPC's companion_data from their personality file.
    target : dict | None
        The current combat target (for aggressive). Must include
        ``ability_scores``, ``level``, ``ac``, ``hp_current``.
    allies : list[dict] | None
        Party members (for supportive). Each must include ``hp_current``, ``hp_max``, ``id``.

    Returns
    -------
    dict
        Action result with ``action_type`` and details.
    """
    if not companion.get("active", True) or companion.get("hp_current", 0) <= 0:
        return {"action_type": "none", "reason": "companion_incapacitated"}

    behavior = companion.get("behavior_type", "defensive")
    combat_profile = companion_data.get("combat_profile", {})
    abilities = combat_profile.get("abilities", [])

    if behavior == "aggressive":
        return _aggressive_action(companion, companion_data, target, abilities)
    if behavior == "supportive":
        return _supportive_action(companion, companion_data, allies, abilities)
    return _defensive_action(companion, companion_data, abilities)


def _aggressive_action(
    companion: dict,
    companion_data: dict,
    target: dict | None,
    abilities: list,
) -> dict:
    if not target:
        return {"action_type": "none", "reason": "no_target"}

    level = companion_data.get("level", 1)
    ability_scores = companion_data.get("ability_scores", {})
    primary_ability = ability_scores.get("strength", 10)
    mod = ability_modifier(primary_ability)
    prof = proficiency_bonus(level)

    # (#2) Use shared roll_d20 for consistency
    roll, dice = roll_d20("straight")
    natural_20 = roll == 20
    natural_1 = roll == 1
    total = roll + mod + prof

    target_ac = target.get("ac", 10)

    if natural_1:
        hit = False
    elif natural_20:
        hit = True
    else:
        hit = total >= target_ac

    damage = 0
    if hit:
        damage_notation = _pick_damage_dice(abilities)
        if natural_20:
            # Double dice count for crits
            parts = damage_notation.split("d")
            count = int(parts[0]) if parts[0] else 1
            damage_notation = f"{count * 2}d{parts[1]}"
        # (#2) Use shared roll_dice for consistency
        dmg_total, _rolls = roll_dice(damage_notation)
        damage = dmg_total + mod

    return {
        "action_type": "attack",
        "roll": roll,
        "dice": dice,
        "total": total,
        "hit": hit,
        "critical": natural_20 and hit,
        "damage": max(0, damage),
        "target_id": target.get("id"),
    }


def _supportive_action(
    companion: dict,
    companion_data: dict,
    allies: list[dict] | None,
    abilities: list,
) -> dict:
    if not allies:
        return {"action_type": "none", "reason": "no_allies"}

    lowest = min(allies, key=lambda a: a.get("hp_current", 0))

    ability_scores = companion_data.get("ability_scores", {})
    primary_ability = ability_scores.get("wisdom", 10)
    mod = ability_modifier(primary_ability)

    healing_notation = _pick_healing_dice(abilities)
    # (#2) Use shared roll_dice for consistency
    heal_total, _rolls = roll_dice(healing_notation)
    healing = heal_total + mod

    return {
        "action_type": "heal",
        "healing": max(1, healing),
        "target_id": lowest.get("id"),
    }


def _defensive_action(
    companion: dict,
    companion_data: dict,
    abilities: list,
) -> dict:
    return {
        "action_type": "protect",
        "companion_id": companion.get("npc_id"),
        "effect": "imposes_disadvantage_on_attacks_against_ally",
    }


def _valid_dice(notation: str) -> bool:
    """Return True if *notation* is a valid dice formula (NdM, NdM+K, flat K)."""
    try:
        parse_formula(notation)
        return True
    except (ValueError, TypeError):
        return False


def _pick_damage_dice(abilities: list) -> str:
    """Extract damage dice from the first attack ability, default 1d6.

    Parses abilities that are dicts with a ``damage_dice`` key.
    Falls back to 1d6 if no ability provides valid dice.
    """
    for ability in abilities:
        if isinstance(ability, dict) and "damage_dice" in ability:
            dice = ability["damage_dice"]
            if _valid_dice(dice):
                return dice
            logger.warning("Invalid damage_dice notation %r, skipping", dice)
    return "1d6"


def _pick_healing_dice(abilities: list) -> str:
    """Extract healing dice from the first healing ability, default 1d8.

    Parses abilities that are dicts with a ``healing_dice`` key.
    Falls back to 1d8 if no ability provides valid dice.
    """
    for ability in abilities:
        if isinstance(ability, dict) and "healing_dice" in ability:
            dice = ability["healing_dice"]
            if _valid_dice(dice):
                return dice
            logger.warning("Invalid healing_dice notation %r, skipping", dice)
    return "1d8"


def apply_directive(
    companion: dict,
    directive: str,
    directive_vocabulary: dict[str, str] | None = None,
) -> dict:
    """Map a prose directive to a behavior_type change.

    Returns the updated companion dict.
    """
    vocab = directive_vocabulary or {}
    mapped = vocab.get(directive)
    if mapped and mapped in ("aggressive", "supportive", "defensive"):
        companion["behavior_type"] = mapped
        logger.info(
            "Companion directive applied",
            extra={"npc_id": companion.get("npc_id"), "directive": directive, "new_behavior": mapped},
        )
    return companion
