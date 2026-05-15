"""Combat resolver — attack rolls, AC computation, damage, saving throws.

Invariant #8: LLM is never authoritative over mechanical state.
Invariant #21: Damage type resistances applied after damage roll.
"""

from __future__ import annotations

import logging
import random

from relay.checks.resolver import ability_modifier, proficiency_bonus, roll_d20

logger = logging.getLogger(__name__)

DAMAGE_TYPES = frozenset(
    {
        "slashing",
        "piercing",
        "bludgeoning",
        "fire",
        "cold",
        "lightning",
        "thunder",
        "poison",
        "acid",
        "necrotic",
        "radiant",
        "force",
        "psychic",
    }
)

ARMOUR_DEX_CAP: dict[str, int | None] = {
    "none": None,
    "light": None,
    "medium": 2,
    "heavy": 0,
}

# (#8) Data-driven environmental effects on combat rolls.
# Keys are scene-state effect IDs; values map "attack" and/or "defense" to
# "advantage" or "disadvantage".  The attack endpoint consults this lookup
# instead of hardcoded if/elif branches.
ENVIRONMENT_COMBAT_EFFECTS: dict[str, dict[str, str]] = {
    "darkness": {"attack": "disadvantage", "defense": "advantage"},
    "high_ground": {"attack": "advantage"},
    "difficult_terrain": {},  # movement cost only — no attack modifier
    "extreme_weather": {"attack": "disadvantage"},
    "hazardous_surface": {},  # damage on entry/start of turn — handled separately
}


def compute_ac(
    equipped_gear: dict[str, str],
    item_lookup: dict[str, dict],
    ability_scores: dict[str, int],
) -> int:
    """Compute AC from equipped gear and DEX modifier.

    AC = 10 + armour_bonus + DEX_mod (capped) + shield_bonus + magical_modifiers.
    """
    base_ac = 10
    armour_bonus = 0
    shield_bonus = 0
    magical_bonus = 0
    armour_category = "none"

    armour_id = equipped_gear.get("armour") or equipped_gear.get("armor")
    if armour_id and armour_id in item_lookup:
        armour = item_lookup[armour_id]
        stats = armour.get("stats", {})
        armour_bonus = stats.get("ac_bonus", 0)
        armour_category = stats.get("armour_category", "none")
        magical_bonus += stats.get("magical_bonus", 0)

    shield_id = equipped_gear.get("shield")
    if shield_id and shield_id in item_lookup:
        shield = item_lookup[shield_id]
        stats = shield.get("stats", {})
        shield_bonus = stats.get("ac_bonus", 2)
        magical_bonus += stats.get("magical_bonus", 0)

    dex_mod = ability_modifier(ability_scores.get("dexterity", 10))
    dex_cap = ARMOUR_DEX_CAP.get(armour_category)
    if dex_cap is not None:
        dex_mod = min(dex_mod, dex_cap)

    total_ac = base_ac + armour_bonus + dex_mod + shield_bonus + magical_bonus
    return max(total_ac, 0)


def roll_dice(notation: str) -> tuple[int, list[int]]:
    """Roll dice from notation like '2d6', '1d8', '3d10'. Returns (total, individual_rolls)."""
    notation = notation.strip().lower()
    if "d" not in notation:
        val = int(notation)
        return val, [val]

    parts = notation.split("d")
    count = int(parts[0]) if parts[0] else 1
    sides = int(parts[1])
    rolls = [random.randint(1, sides) for _ in range(count)]
    return sum(rolls), rolls


def attack_roll(
    attacker_ability_scores: dict[str, int],
    attacker_level: int,
    weapon: dict,
    *,
    proficient: bool = True,
    advantage: bool = False,
    disadvantage: bool = False,
) -> dict:
    """Resolve an attack roll. Returns roll details and the total."""
    stats = weapon.get("stats", {})
    modifier_source = stats.get("modifier_source", "strength")
    ability_mod = ability_modifier(attacker_ability_scores.get(modifier_source, 10))
    prof = proficiency_bonus(attacker_level) if proficient else 0

    # Advantage/disadvantage cancellation
    if advantage and disadvantage:
        mode = "straight"
    elif advantage:
        mode = "advantage"
    elif disadvantage:
        mode = "disadvantage"
    else:
        mode = "straight"

    # (#4) Use shared roll_d20 from checks.resolver
    roll, dice = roll_d20(mode)

    natural_20 = roll == 20
    natural_1 = roll == 1
    total = roll + ability_mod + prof

    result = {
        "roll": roll,
        "dice": dice,
        "roll_mode": mode,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof,
        "total": total,
        "natural_20": natural_20,
        "natural_1": natural_1,
    }

    logger.info(
        "Attack roll resolved",
        extra={
            "roll": roll,
            "roll_mode": mode,
            "ability_modifier": ability_mod,
            "proficiency_bonus": prof,
            "total": total,
            "natural_20": natural_20,
            "natural_1": natural_1,
        },
    )
    return result


def resolve_attack(
    attack: dict,
    target_ac: int,
) -> dict:
    """Determine hit/miss/crit from an attack roll result against target AC."""
    if attack["natural_1"]:
        result = {**attack, "hit": False, "critical": False, "auto_miss": True}
    elif attack["natural_20"]:
        result = {**attack, "hit": True, "critical": True, "auto_miss": False}
    else:
        hit = attack["total"] >= target_ac
        result = {**attack, "hit": hit, "critical": False, "auto_miss": False}

    logger.info(
        "Attack resolved",
        extra={
            "hit": result["hit"],
            "critical": result["critical"],
            "target_ac": target_ac,
            "total": attack["total"],
        },
    )
    return result


def damage_roll(
    weapon: dict,
    attacker_ability_scores: dict[str, int],
    *,
    critical: bool = False,
) -> dict:
    """Roll damage for a weapon hit. Critical doubles dice count, not modifier."""
    stats = weapon.get("stats", {})
    damage_dice = stats.get("damage_dice", "1d6")
    damage_type = stats.get("damage_type", "bludgeoning")
    modifier_source = stats.get("modifier_source", "strength")
    ability_mod = ability_modifier(attacker_ability_scores.get(modifier_source, 10))

    # (#11) Validate damage type against registry
    if damage_type not in DAMAGE_TYPES:
        logger.warning(
            "Invalid damage type, defaulting to bludgeoning",
            extra={"proposed": damage_type},
        )
        damage_type = "bludgeoning"

    notation = damage_dice.strip().lower()
    parts = notation.split("d")
    count = int(parts[0]) if parts[0] else 1
    sides = int(parts[1])

    if critical:
        count *= 2

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + ability_mod

    result = {
        "damage_dice": f"{count}d{sides}",
        "rolls": rolls,
        "ability_modifier": ability_mod,
        "raw_total": total,
        "damage_type": damage_type,
        "critical": critical,
    }

    logger.info(
        "Damage roll resolved",
        extra={
            "damage_dice": f"{count}d{sides}",
            "damage_type": damage_type,
            "raw_total": total,
            "critical": critical,
        },
    )
    return result


def apply_damage_resistances(
    damage: dict,
    resistances: list[str] | None = None,
    vulnerabilities: list[str] | None = None,
    immunities: list[str] | None = None,
) -> dict:
    """Apply damage type multipliers after the damage roll (Invariant #21)."""
    raw = damage["raw_total"]
    dtype = damage["damage_type"]
    multiplier = 1.0

    if immunities and dtype in immunities:
        multiplier = 0.0
    elif resistances and dtype in resistances:
        multiplier = 0.5
    elif vulnerabilities and dtype in vulnerabilities:
        multiplier = 2.0

    final_damage = max(0, int(raw * multiplier))

    return {
        **damage,
        "multiplier": multiplier,
        "final_damage": final_damage,
    }


def saving_throw(
    defender_ability_scores: dict[str, int],
    defender_level: int,
    defender_save_proficiencies: list[str],
    save_type: str,
    dc: int,
    *,
    advantage: bool = False,
    disadvantage: bool = False,
) -> dict:
    """Resolve a saving throw. Returns roll details and pass/fail.

    (#5) Natural 20 always succeeds; natural 1 always fails.
    """
    ability_mod = ability_modifier(defender_ability_scores.get(save_type, 10))
    prof = proficiency_bonus(defender_level) if save_type in defender_save_proficiencies else 0

    if advantage and disadvantage:
        mode = "straight"
    elif advantage:
        mode = "advantage"
    elif disadvantage:
        mode = "disadvantage"
    else:
        mode = "straight"

    # (#4) Use shared roll_d20 from checks.resolver
    roll, dice = roll_d20(mode)

    total = roll + ability_mod + prof

    # (#5) Natural 20/1 auto-pass/fail on saving throws
    natural_20 = roll == 20
    natural_1 = roll == 1

    if natural_20:
        passed = True
    elif natural_1:
        passed = False
    else:
        passed = total >= dc

    result = {
        "save_type": save_type,
        "dc": dc,
        "roll": roll,
        "dice": dice,
        "roll_mode": mode,
        "ability_modifier": ability_mod,
        "proficiency_bonus": prof,
        "total": total,
        "passed": passed,
        "natural_20": natural_20,
        "natural_1": natural_1,
    }

    logger.info(
        "Saving throw resolved",
        extra={
            "save_type": save_type,
            "dc": dc,
            "roll": roll,
            "roll_mode": mode,
            "total": total,
            "passed": passed,
            "natural_20": natural_20,
            "natural_1": natural_1,
        },
    )
    return result


def compute_save_dc(
    attacker_ability_scores: dict[str, int],
    attacker_level: int,
    dc_source_ability: str,
) -> int:
    """Compute save DC: 8 + ability modifier + proficiency bonus."""
    ability_mod = ability_modifier(attacker_ability_scores.get(dc_source_ability, 10))
    prof = proficiency_bonus(attacker_level)
    return 8 + ability_mod + prof
