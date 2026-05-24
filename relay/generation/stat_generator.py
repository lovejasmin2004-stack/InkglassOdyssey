"""Mechanical stat generation for Tier 2 NPCs.

Stats come from templates and rules, NOT the LLM (Invariant #8).
The LLM provides narrative flavor; the relay provides the numbers.

Design doc: docs/design_proposals.md §1 (Three-Tier Content System)
"""

from __future__ import annotations

import random

from relay.generation.template_loader import NpcTemplate

# Canonical ability scores (D&D-standard order)
_ALL_ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]

# Score ranges by profile tier
_PRIMARY_RANGE = (14, 16)
_SECONDARY_RANGE = (11, 13)
_DUMP_RANGE = (8, 10)
_DEFAULT_RANGE = (10, 12)


def generate_ability_scores(template: NpcTemplate, *, rng: random.Random | None = None) -> dict[str, int]:
    """Generate ability scores based on the template's profile.

    Primary abilities get 14-16, secondary get 11-13, dump get 8-10,
    everything else gets 10-12.  Uses an optional seeded Random for
    deterministic testing.
    """
    rng = rng or random.Random()
    profile = template.ability_score_profile

    primary = set(profile.primary)
    secondary = set(profile.secondary)
    dump = set(profile.dump)

    scores: dict[str, int] = {}
    for ability in _ALL_ABILITIES:
        if ability in primary:
            scores[ability] = rng.randint(*_PRIMARY_RANGE)
        elif ability in secondary:
            scores[ability] = rng.randint(*_SECONDARY_RANGE)
        elif ability in dump:
            scores[ability] = rng.randint(*_DUMP_RANGE)
        else:
            scores[ability] = rng.randint(*_DEFAULT_RANGE)

    return scores


def ability_modifier(score: int) -> int:
    """Standard D&D ability modifier: (score - 10) // 2."""
    return (score - 10) // 2


def generate_hp_max(
    level: int,
    hit_die: int,
    con_score: int,
) -> int:
    """Generate HP max from level, hit die, and Constitution.

    Formula (from docs/combat system.pdf):
      HP = hit_die + (level - 1) * (hit_die/2 + 1) + level * CON modifier

    First level gets max hit die, subsequent levels get average.
    """
    con_mod = ability_modifier(con_score)
    first_level_hp = hit_die
    subsequent_hp = max(0, level - 1) * (hit_die // 2 + 1)
    con_contribution = level * con_mod

    return max(1, first_level_hp + subsequent_hp + con_contribution)


def generate_ac(template: NpcTemplate, *, rng: random.Random | None = None) -> int:
    """Pick AC from the template's range."""
    rng = rng or random.Random()
    lo, hi = template.ac_range
    return rng.randint(lo, hi)


def pick_level(template: NpcTemplate, *, rng: random.Random | None = None) -> int:
    """Pick a level from the template's range."""
    rng = rng or random.Random()
    lo, hi = template.level_range
    return rng.randint(lo, hi)


def pick_skill_proficiencies(template: NpcTemplate, *, rng: random.Random | None = None) -> list[str]:
    """Pick skills from the template's pool."""
    rng = rng or random.Random()
    pool = list(template.skill_proficiency_pool)
    count = min(template.skill_proficiency_count, len(pool))
    return sorted(rng.sample(pool, count))
