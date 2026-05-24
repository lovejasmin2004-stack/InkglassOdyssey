"""NPC generator — creates schema-valid Tier 2 NPCs from templates.

Mechanical stats come from templates and rules (Invariant #8).
Narrative flavor (name, personality, goals) comes from template pools
and randomization.  LLM enhancement is optional (Phase 2).

The generator produces a fully valid NpcPersonality that can be loaded
by the existing npc_loader, persisted to disk, and used in scenes.

Design doc: docs/design_proposals.md §1 (Three-Tier Content System)
"""

from __future__ import annotations

import logging
import random
import uuid

from pydantic import ValidationError

from relay.generation.stat_generator import (
    generate_ability_scores,
    generate_ac,
    generate_hp_max,
    pick_level,
    pick_skill_proficiencies,
)
from relay.generation.template_loader import NpcTemplate
from relay.schemas import NpcPersonality

logger = logging.getLogger(__name__)


class GenerationError(Exception):
    """NPC generation failed."""


def generate_npc(
    template: NpcTemplate,
    region_id: str,
    *,
    rng: random.Random | None = None,
    npc_id: str | None = None,
) -> NpcPersonality:
    """Generate a schema-valid NPC from a template and region.

    Parameters
    ----------
    template
        The NpcTemplate defining role, stat ranges, and narrative pools.
    region_id
        The region where this NPC is being placed.
    rng
        Optional seeded Random for deterministic testing.
    npc_id
        Optional explicit ID.  If not provided, generates a UUID-based one.

    Returns
    -------
    NpcPersonality
        A fully schema-valid NPC personality, marked with generated=True.

    Raises
    ------
    GenerationError
        If the generated NPC fails schema validation (should not happen
        with valid templates — this is a safety net).
    """
    rng = rng or random.Random()

    # --- Identity ---
    name = rng.choice(template.name_pool)
    npc_id = npc_id or f"gen_{template.id}_{uuid.uuid4().hex[:8]}"

    # --- Mechanical stats ---
    level = pick_level(template, rng=rng)
    ability_scores = generate_ability_scores(template, rng=rng)
    ac = generate_ac(template, rng=rng)
    hp_max = generate_hp_max(level, template.hit_die, ability_scores.get("constitution", 10))
    skill_profs = pick_skill_proficiencies(template, rng=rng)

    # --- Personality (from template pools, no LLM) ---
    traits = rng.sample(template.personality_trait_pool, min(2, len(template.personality_trait_pool)))
    trait_str = " and ".join(traits)

    personality_background = (
        f"A {trait_str} {template.role_display.lower()} found in the {region_id.replace('_', ' ')} area."
    )

    goals = {
        "immediate": [f"Carry out {template.role_display.lower()} duties in {region_id.replace('_', ' ')}."],
        "long_term": [f"Continue working as a {template.role_display.lower()}."],
    }

    weaknesses_fears = f"A {template.role_display.lower()} with typical concerns for someone in their position."

    communication_style = f"Speaks in the manner typical of a {trait_str} {template.role_display.lower()}."

    power_narrative = f"Has the influence and resources typical of a {template.role_display.lower()} in this region."

    # --- Knowledge boundaries ---
    knows = (
        list(template.knowledge_scope)
        if template.knowledge_scope
        else [f"Matters relevant to a {template.role_display.lower()}"]
    )
    does_not_know = (
        list(template.ignorance_scope) if template.ignorance_scope else ["Topics outside their area of expertise"]
    )

    # --- Secrets (minimal, valid) ---
    secrets = [
        {
            "content": f"{name} has a personal matter they keep to themselves.",
            "reveal_condition": "relationship_threshold",
            "secret_type": "information",
            "reveal_threshold": 40,
        }
    ]

    # --- Few-shot examples (from template, with name substitution) ---
    few_shot_examples = []
    for fst in template.few_shot_templates:
        few_shot_examples.append(
            {
                "player_input": fst.player_input,
                "npc_response": fst.npc_response_template.replace("{name}", name),
                "context_tag": fst.context_tag,
            }
        )

    # --- Manipulation resistance (from template) ---
    manip_examples = []
    for mrt in template.manipulation_resistance_templates:
        manip_examples.append(
            {
                "player_input": mrt.player_input,
                "npc_refusal": mrt.npc_refusal_template.replace("{name}", name),
            }
        )

    # --- Animation profile (from template defaults) ---
    anim = template.animation_profile_defaults
    animation_profile = {
        "default_stance": anim.default_stance,
        "default_gaze": anim.default_gaze,
        "emotional_state_to_animation": dict(anim.emotional_state_to_animation),
    }

    # --- Assemble NPC data dict ---
    npc_data = {
        "id": npc_id,
        "world_id": template.world_id,
        "name": name,
        "entity_class": template.entity_class,
        "role": template.role_display,
        "level": level,
        "hit_die": template.hit_die,
        "personality_background": personality_background,
        "goals": goals,
        "weaknesses_fears": weaknesses_fears,
        "communication_style": communication_style,
        "power_narrative": power_narrative,
        "knowledge_boundaries": {"knows": knows, "does_not_know": does_not_know},
        "relationships": [],
        "secrets": secrets,
        "few_shot_examples": few_shot_examples,
        "manipulation_resistance_examples": manip_examples,
        "animation_profile": animation_profile,
        "world_position": {"region_id": region_id},
        "ability_scores": ability_scores,
        "ac": ac,
        "saving_throw_proficiencies": list(template.saving_throw_proficiencies),
        "skill_proficiencies": skill_profs,
        "hp_max": hp_max,
        "generated": True,
        "source_template_id": template.id,
    }

    # Add optional fields
    if template.faction_affinity:
        npc_data["faction_id"] = template.faction_affinity
    if template.consequence_profile:
        npc_data["consequence_profile"] = template.consequence_profile

    # --- Validate against NPC schema (safety net) ---
    try:
        npc = NpcPersonality.model_validate(npc_data)
    except ValidationError as exc:
        logger.error(
            "Generated NPC failed schema validation",
            extra={"npc_id": npc_id, "template_id": template.id, "error": str(exc)},
        )
        raise GenerationError(
            f"Generated NPC '{npc_id}' from template '{template.id}' failed schema validation: {exc}"
        ) from exc

    logger.info(
        "NPC generated",
        extra={
            "npc_id": npc_id,
            "npc_name": name,
            "template_id": template.id,
            "npc_level": level,
            "region_id": region_id,
        },
    )

    return npc
