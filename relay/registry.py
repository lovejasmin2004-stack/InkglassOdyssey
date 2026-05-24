"""Canonical registries for the rules layer.

Single source of truth for: ability names, skill list, condition definitions,
damage types, environmental effects. Per-world display names live in
world_config.json; this module holds the mechanical IDs.

Mirrors the role of Foundry dnd5e's `module/config.mjs`.
"""

from __future__ import annotations

from typing import Literal

# ---------------------------------------------------------------------------
# Abilities and skills
# ---------------------------------------------------------------------------

ABILITIES: tuple[str, ...] = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)
"""Six canonical ability IDs. Per-world display names overlay these."""


SKILL_ABILITY: dict[str, str] = {
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
"""Skill ID → governing ability ID. Mechanical mapping; not localized."""


SKILLS: frozenset[str] = frozenset(SKILL_ABILITY.keys())
"""All canonical skill IDs."""


PASSIVE_SKILLS: tuple[str, ...] = ("perception", "insight", "investigation")
"""Skills evaluated as passive checks against hidden scene elements (Invariant #22)."""


SOCIAL_SKILLS: frozenset[str] = frozenset({"intimidation", "persuasion", "deception", "performance"})
"""Skills affected by the charmed condition's social advantage."""


# ---------------------------------------------------------------------------
# Damage types
# ---------------------------------------------------------------------------

DAMAGE_TYPES: tuple[str, ...] = (
    "bludgeoning",
    "piercing",
    "slashing",
    "fire",
    "cold",
    "lightning",
    "thunder",
    "acid",
    "poison",
    "necrotic",
    "radiant",
    "psychic",
    "force",
)
"""Canonical damage type IDs. Items, abilities, and damage terms must use these."""


HEALING_TYPES: tuple[str, ...] = ("healing", "temp_hp")
"""Non-damage outcomes that share the damage-term shape."""


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

DurationUnit = Literal["rounds", "turns", "minutes", "until_long_rest", "permanent"]
"""How the duration_remaining field is interpreted."""


SourceType = Literal["spell", "feature", "environment", "item", "scenario", "other"]
"""What kind of effect applied a condition."""


class ConditionDef:
    """Static description of a condition's mechanical effects.

    Foundry dnd5e calls these "status effects" — definitions live in config,
    instances live as Active Effects on actors. We mirror that split: defs
    here, instances on Character.conditions (or future Combatant.conditions).
    """

    __slots__ = (
        "auto_fail_dexterity_saves",
        "auto_fail_strength_saves",
        "disadvantage_on_all_checks",
        "disadvantage_on_attacks",
        "disadvantage_on_saves",
        "disadvantage_on_skills",
        "grants_advantage_to_attackers",
        "id",
        "incapacitated",
        "range_dependent_advantage",
        "requires_source_visible",
        "rider_conditions",
        "speed_zero",
    )

    def __init__(
        self,
        condition_id: str,
        *,
        rider_conditions: tuple[str, ...] = (),
        disadvantage_on_all_checks: bool = False,
        disadvantage_on_skills: tuple[str, ...] = (),
        disadvantage_on_attacks: bool = False,
        disadvantage_on_saves: tuple[str, ...] = (),
        grants_advantage_to_attackers: bool = False,
        auto_fail_strength_saves: bool = False,
        auto_fail_dexterity_saves: bool = False,
        speed_zero: bool = False,
        incapacitated: bool = False,
        range_dependent_advantage: str | None = None,
        requires_source_visible: bool = False,
    ) -> None:
        self.id = condition_id
        self.rider_conditions = rider_conditions
        self.disadvantage_on_all_checks = disadvantage_on_all_checks
        self.disadvantage_on_skills = disadvantage_on_skills
        self.disadvantage_on_attacks = disadvantage_on_attacks
        self.disadvantage_on_saves = disadvantage_on_saves
        self.grants_advantage_to_attackers = grants_advantage_to_attackers
        self.auto_fail_strength_saves = auto_fail_strength_saves
        self.auto_fail_dexterity_saves = auto_fail_dexterity_saves
        self.speed_zero = speed_zero
        self.incapacitated = incapacitated
        self.range_dependent_advantage = range_dependent_advantage
        self.requires_source_visible = requires_source_visible


CONDITIONS: dict[str, ConditionDef] = {
    "blinded": ConditionDef(
        "blinded",
        disadvantage_on_attacks=True,
        grants_advantage_to_attackers=True,
    ),
    "charmed": ConditionDef("charmed"),
    "deafened": ConditionDef("deafened"),
    "frightened": ConditionDef(
        "frightened",
        disadvantage_on_all_checks=True,
        disadvantage_on_attacks=True,
        requires_source_visible=True,
    ),
    "grappled": ConditionDef("grappled", speed_zero=True),
    "incapacitated": ConditionDef("incapacitated", incapacitated=True),
    "invisible": ConditionDef("invisible"),
    "paralyzed": ConditionDef(
        "paralyzed",
        rider_conditions=("incapacitated",),
        speed_zero=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        grants_advantage_to_attackers=True,
    ),
    "petrified": ConditionDef(
        "petrified",
        rider_conditions=("incapacitated",),
        speed_zero=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        grants_advantage_to_attackers=True,
    ),
    "poisoned": ConditionDef(
        "poisoned",
        disadvantage_on_all_checks=True,
        disadvantage_on_attacks=True,
    ),
    "prone": ConditionDef(
        "prone",
        disadvantage_on_attacks=True,
        grants_advantage_to_attackers=True,
        range_dependent_advantage="melee_only",
    ),
    "restrained": ConditionDef(
        "restrained",
        disadvantage_on_skills=("acrobatics", "athletics", "stealth"),
        disadvantage_on_attacks=True,
        disadvantage_on_saves=("dexterity",),
        speed_zero=True,
        grants_advantage_to_attackers=True,
    ),
    "stunned": ConditionDef(
        "stunned",
        rider_conditions=("incapacitated",),
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        grants_advantage_to_attackers=True,
    ),
    "unconscious": ConditionDef(
        "unconscious",
        rider_conditions=("incapacitated", "prone"),
        speed_zero=True,
        auto_fail_strength_saves=True,
        auto_fail_dexterity_saves=True,
        grants_advantage_to_attackers=True,
    ),
}
"""Standard D&D-inspired conditions. Inkglass-specific conditions can extend this dict."""


EXHAUSTION_MAX = 6


def exhaustion_def(level: int) -> ConditionDef:
    """Exhaustion is graduated 1-6.

    Level 1+: disadvantage on ability checks.
    Level 3+: disadvantage on attack rolls and saving throws.
    Level 5+: speed zero.
    Level 6: character retired (handled by death_state module).
    """
    level = max(1, min(EXHAUSTION_MAX, level))
    return ConditionDef(
        f"exhaustion_{level}",
        disadvantage_on_all_checks=True,
        disadvantage_on_attacks=level >= 3,
        disadvantage_on_saves=("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")
        if level >= 3
        else (),
        speed_zero=level >= 5,
    )


# ---------------------------------------------------------------------------
# Environmental effects
# ---------------------------------------------------------------------------

ENVIRONMENT_RULES: dict[str, dict[str, str]] = {
    "darkness": {"perception": "disadvantage"},
    "difficult_terrain": {"acrobatics": "disadvantage", "stealth": "disadvantage"},
    "extreme_weather": {"perception": "disadvantage", "survival": "disadvantage"},
}
"""Scene-state environmental effects → skill-specific advantage/disadvantage."""
