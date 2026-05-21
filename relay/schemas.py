from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from relay.registry import DAMAGE_TYPES

# ---------------------------------------------------------------------------
# Error response (CLAUDE.md §8.1)
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Canonical error envelope returned by all endpoints.

    Matches CLAUDE.md §8.1: code (string), message (string),
    turn_id (optional), narrative_hint (optional).
    """

    code: str
    message: str
    turn_id: str | None = None
    narrative_hint: str | None = None


# ---------------------------------------------------------------------------
# Reusable field types
# ---------------------------------------------------------------------------

_DICE_FORMULA_RE = re.compile(r"^\d+d\d+([+-]\d+)?$|^[+-]?\d+$")

DiceFormulaStr = Annotated[
    str,
    StringConstraints(pattern=_DICE_FORMULA_RE.pattern, strip_whitespace=True),
]
"""'NdM', 'NdM+K', 'NdM-K', or flat 'K'. Validated by combat.damage.parse_formula."""

DamageType = Literal[
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
]
"""Canonical damage type IDs; mirrors relay.registry.DAMAGE_TYPES."""

assert set(DamageType.__args__) == set(DAMAGE_TYPES), "DamageType / DAMAGE_TYPES drift"

# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------


class ConditionEntry(BaseModel):
    """A condition instance on a character / combatant.

    Schema mirrors Foundry's ActiveEffect pattern: a static definition lives in
    relay.registry.CONDITIONS; instances carry duration and provenance.
    """

    condition_id: str
    instance_id: str | None = None
    duration_remaining: int | None = Field(default=None, ge=0)
    duration_unit: Literal["rounds", "turns", "minutes", "until_long_rest", "permanent"] = "turns"
    rider_of: str | None = None
    source: str
    source_type: Literal["spell", "feature", "environment", "item", "scenario", "other"] = "other"

    # Legacy fields retained for backward compatibility with stored JSON.
    duration_turns: int | None = Field(default=None, ge=0)
    expiry_turn: int | None = Field(default=None, ge=0)


class DamageTermEntry(BaseModel):
    """One typed damage term. Multi-part damage is a list of these."""

    formula: DiceFormulaStr
    type: DamageType


class InventoryEntry(BaseModel):
    item_id: str
    quantity: int = Field(ge=1)
    binding_state: Literal["unbound", "bound"]


class CompanionEntry(BaseModel):
    npc_id: str
    behavior_type: Literal["aggressive", "supportive", "defensive"]
    loyalty_strain: int = Field(ge=0)
    hp_current: int = Field(ge=0)
    hp_max: int = Field(ge=1)
    conditions: list[str] = Field(default_factory=list)
    exhaustion_level: int = Field(ge=0, le=6, default=0)
    active: bool = True


class ResourceEntry(BaseModel):
    current: int = Field(ge=0)
    max: int = Field(ge=0)


# ---------------------------------------------------------------------------
# CharacterSheet
# ---------------------------------------------------------------------------


class CharacterSheet(BaseModel):
    id: str
    player_id: str
    world_id: str
    name: str = Field(min_length=1)
    level: int = Field(ge=1, le=20)
    specialisation_path_id: str
    ability_scores: dict[str, int]
    skill_proficiencies: list[str]
    saving_throw_proficiencies: list[str] = Field(min_length=2, max_length=2)
    hp_current: int
    hp_max: int = Field(ge=1)
    ac: int = Field(ge=0)
    passive_checks: dict[str, int]
    conditions: list[ConditionEntry]
    exhaustion_level: int = Field(ge=0, le=6)
    resources: dict[str, ResourceEntry]
    wallet: dict[str, int]
    inventory: list[InventoryEntry]
    equipped_gear: dict[str, str]
    known_recipes: list[str]
    companions: list[CompanionEntry]
    rp_voice_notes: str | None = None
    relationships: dict[str, int]
    faction_standing: dict[str, int]
    created_at: datetime
    updated_at: datetime

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# NPC Personality sub-models
# ---------------------------------------------------------------------------


class NpcGoals(BaseModel):
    immediate: list[str] = Field(min_length=1)
    long_term: list[str]


class NpcKnowledgeBoundaries(BaseModel):
    knows: list[str]
    does_not_know: list[str]


class NpcRelationship(BaseModel):
    npc_id: str
    relationship_type: Literal["ally", "rival", "subordinate", "mentor", "family", "trading_partner", "unknown"]
    description: str


class NpcSecret(BaseModel):
    content: str
    reveal_condition: Literal["relationship_threshold", "quest_flag", "check_type_and_dc", "never"]
    secret_type: Literal["information", "identity"]
    reveal_threshold: int | None = None
    reveal_quest_flag: str | None = None
    reveal_check_type: str | None = None
    reveal_check_dc: int | None = None


class FewShotExample(BaseModel):
    player_input: str
    npc_response: str
    context_tag: Literal["casual", "tense", "hostile", "transactional"]


class ManipulationResistanceExample(BaseModel):
    player_input: str
    npc_refusal: str


class AnimationProfile(BaseModel):
    default_stance: str
    default_gaze: str
    emotional_state_to_animation: dict[str, str] = Field(min_length=3)
    movement_triggers: list[str] | None = None


class WorldPosition(BaseModel):
    region_id: str
    coordinates: dict[str, float] | None = None


class NpcScheduleEntry(BaseModel):
    time_range: str
    position: str


class ShopInventoryEntry(BaseModel):
    item_id: str
    stock_quantity: int = Field(ge=0)
    markup_percentage: float


class AccessPrerequisites(BaseModel):
    faction_standing_threshold: int | None = None
    level_requirement: int | None = None
    quest_flags: list[str] | None = None

    model_config = {"extra": "forbid"}


class ShopData(BaseModel):
    inventory: list[ShopInventoryEntry]
    pricing_policy: str
    restock_schedule: Literal["daily", "weekly", "never"]
    access_prerequisites: AccessPrerequisites | None = None


class CompanionRecruitment(BaseModel):
    affection_threshold: int
    recruitment_scenario_id: str
    recruitment_conditions: list[str] | None = None


class CompanionAbility(BaseModel):
    """A single companion combat ability with optional dice formulas."""

    name: str = Field(min_length=1)
    damage_dice: DiceFormulaStr | None = None
    healing_dice: DiceFormulaStr | None = None


class CompanionCombatProfile(BaseModel):
    behavior_type: Literal["aggressive", "supportive", "defensive"]
    abilities: list[CompanionAbility] = Field(min_length=2, max_length=3)
    directive_vocabulary: dict[str, str] | None = None


class CompanionAmbientBehavior(BaseModel):
    comment_frequency: str
    trigger_categories: list[str]
    mood_modifier: float | None = None


class CompanionData(BaseModel):
    recruitment: CompanionRecruitment  # TODO(phase-3): wire recruitment_scenario_id to scenario endpoint
    combat_profile: CompanionCombatProfile
    ambient_behavior: CompanionAmbientBehavior
    loyalty_strain_threshold: int
    world_event_reactions: list[dict] | None = None
    farewell_template: str | None = None
    reunion_template: str | None = None  # TODO(phase-4): implement reunion mechanism
    dismissal_relationship_modifier: int | None = None
    confrontation_scene_id: str | None = None


# ---------------------------------------------------------------------------
# NpcPersonality
# ---------------------------------------------------------------------------


class NpcPersonality(BaseModel):
    id: str
    world_id: str
    name: str = Field(min_length=1)
    entity_class: Literal["humanoid", "creature", "spirit", "construct"]
    role: str
    level: int = Field(ge=1, le=20)
    hit_die: Literal[6, 8, 10, 12]

    # Narrative (LLM-facing)
    personality_background: str
    goals: NpcGoals
    weaknesses_fears: str
    communication_style: str
    power_narrative: str
    knowledge_boundaries: NpcKnowledgeBoundaries
    relationships: list[NpcRelationship]
    secrets: list[NpcSecret] = Field(min_length=1)
    few_shot_examples: list[FewShotExample] = Field(min_length=2)
    manipulation_resistance_examples: list[ManipulationResistanceExample] = Field(min_length=1)
    animation_profile: AnimationProfile
    world_position: WorldPosition
    schedule: list[NpcScheduleEntry] | None = None

    # Mechanical stat block (relay-only, never sent to LLM)
    ability_scores: dict[str, int]
    ac: int = Field(ge=0)
    saving_throw_proficiencies: list[str] = Field(min_length=2, max_length=2)
    skill_proficiencies: list[str]
    hp_max: int = Field(ge=1)
    resistances: list[str] | None = None
    vulnerabilities: list[str] | None = None
    immunities: list[str] | None = None
    conditions: list[dict] | None = None
    notable_equipment: list[str] | None = None

    # Faction and conditional sections
    faction_id: str | None = None
    shop_data: ShopData | None = None
    companion_data: CompanionData | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Ability
# ---------------------------------------------------------------------------


class AbilityCost(BaseModel):
    resource_type: str
    amount: int = Field(ge=0)


class AppliesCondition(BaseModel):
    condition_id: str
    duration: int = Field(ge=1)


class Ability(BaseModel):
    id: str
    world_id: str
    name: str = Field(min_length=1)
    description: str
    ability_score: str
    modifier_source: str | None = None
    damage_dice: str | None = None
    damage_type: str | None = None
    healing_dice: str | None = None
    save_type: str | None = None
    save_dc_source: str | None = None
    applies_condition: AppliesCondition | None = None
    cost: AbilityCost
    cooldown_turns: int | None = Field(default=None, ge=1)
    level_requirement: int = Field(ge=1, le=20)
    tags: list[str]

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: str
    world_id: str
    name: str = Field(min_length=1)
    type: Literal["weapon", "armour", "shield", "consumable", "material", "tool", "quest"]
    rarity: Literal["common", "uncommon", "rare", "legendary"]
    weight: float = Field(ge=0)
    value: int = Field(ge=0)
    description_prose: str
    binding: Literal["unbound", "bind_on_equip", "bind_on_acquire"]
    unique: bool
    stats: dict | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------


class RecipeMaterial(BaseModel):
    item_id: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class Recipe(BaseModel):
    id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    output_item_id: str = Field(min_length=1)
    output_quantity: int = Field(default=1, ge=1)
    input_materials: list[RecipeMaterial] = Field(min_length=1)
    required_skill: str = Field(min_length=1)
    skill_dc: int = Field(ge=1, le=30)
    level_requirement: int = Field(ge=1, le=20)
    required_station_type: str | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# WorldConfig sub-models
# ---------------------------------------------------------------------------


class SpecialisationPath(BaseModel):
    id: str
    world: str
    display_name: str
    path_archetype: Literal["scholar", "balanced", "martial", "tank"]
    hit_die: Literal[6, 8, 10, 12]
    saving_throw_proficiencies: list[str] = Field(min_length=2, max_length=2)
    primary_ability_scores: list[str] = Field(min_length=1, max_length=2)
    available_skill_proficiencies: list[str]
    description: str

    model_config = {"extra": "forbid"}


class RestRules(BaseModel):
    short_rest_hp_percent: float
    short_rest_hit_dice_formula: str
    long_rest_hp_percent: float
    long_rest_exhaustion_reduction: int

    model_config = {"extra": "forbid"}


class EconomyConfig(BaseModel):
    sell_back_ratio: float = Field(ge=0, le=1)
    earning_rates: dict | None = None
    price_ranges: dict | None = None
    crafting_margin_target: float | None = None
    transport_fare_ranges: dict | None = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# WorldConfig
# ---------------------------------------------------------------------------


class WorldConfig(BaseModel):
    world_id: str
    display_name: str
    content_rating: Literal["moderate", "mature"]
    rp_system_prompt_addendum: str
    traversal_config: list[str]
    currency_id: str
    ability_score_map: dict[str, str]
    equipment_slots: list[str] | None = None
    resource_model: dict | None = None
    time_of_day_cycle: dict | None = None
    max_active_companions: int = Field(ge=1, default=1)
    specialisation_paths: list[SpecialisationPath] = Field(min_length=1)
    rest_rules: RestRules
    economy_config: EconomyConfig
    environmental_effects_registry: list[dict] | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Faction
# ---------------------------------------------------------------------------


class ReputationThresholds(BaseModel):
    hostile: int
    unfriendly: int
    neutral: int
    friendly: int
    allied: int

    @model_validator(mode="after")
    def _check_ordering(self) -> ReputationThresholds:
        """Thresholds must be strictly ordered: hostile < unfriendly ≤ neutral ≤ friendly < allied."""
        if not (self.hostile < self.unfriendly <= self.neutral <= self.friendly < self.allied):
            raise ValueError(
                f"Reputation thresholds must be ordered "
                f"hostile({self.hostile}) < unfriendly({self.unfriendly}) "
                f"<= neutral({self.neutral}) <= friendly({self.friendly}) "
                f"< allied({self.allied})"
            )
        return self


class TierModifiers(BaseModel):
    """Per-tier price multipliers (docs/faction system.pdf).

    Values are direct multipliers: 0.80 means "pay 80% of base+markup"
    (a 20% discount). When a tier is None the global default applies.
    """

    allied: float | None = None
    friendly: float | None = None
    neutral: float | None = None
    unfriendly: float | None = None

    model_config = {"extra": "forbid"}


class ShopPriceModifiers(BaseModel):
    """Per-faction price multipliers for buy and sell sides.

    ``buy``: multipliers applied to the markup-adjusted buy price.
    ``sell``: multipliers applied to the sell-back price.
    When not provided on a faction, the global defaults in pricing.py apply.
    """

    buy: TierModifiers | None = None
    sell: TierModifiers | None = None

    model_config = {"extra": "forbid"}


class Faction(BaseModel):
    id: str = Field(min_length=1)
    world_id: str | None = None
    name: str = Field(min_length=1)
    allied_factions: list[str]
    rival_factions: list[str]
    reputation_thresholds: ReputationThresholds
    description: str
    shop_price_modifiers: ShopPriceModifiers | None = None
    notable_npcs: list[str] | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _no_ally_rival_overlap(self) -> Faction:
        """A faction cannot appear in both allied_factions and rival_factions."""
        overlap = set(self.allied_factions) & set(self.rival_factions)
        if overlap:
            raise ValueError(f"Faction cannot be both allied and rival: {sorted(overlap)}")
        return self


# ---------------------------------------------------------------------------
# Region
# ---------------------------------------------------------------------------


class GatheringNode(BaseModel):
    item_id: str = Field(min_length=1)
    skill: str = Field(min_length=1)
    dc: int = Field(ge=1, le=30)
    yield_min: int | None = Field(default=None, ge=1, le=20)
    yield_max: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_node(self) -> GatheringNode:
        from relay.registry import SKILLS

        if self.skill not in SKILLS:
            raise ValueError(f"Unknown gathering skill '{self.skill}'; valid: {sorted(SKILLS)}")
        if self.yield_min is not None and self.yield_max is not None and self.yield_min > self.yield_max:
            raise ValueError(f"yield_min ({self.yield_min}) must be <= yield_max ({self.yield_max})")
        return self


class LevelRange(BaseModel):
    min: int = Field(ge=1, le=20)
    max: int = Field(ge=1, le=20)


class Region(BaseModel):
    id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    connections: list[str] = Field(default_factory=list)
    environmental_effects: list[str] = Field(default_factory=list)
    gathering_nodes: list[GatheringNode] | None = None
    fauna: list[str] | None = None
    dominant_faction: str | None = None
    traversal_modes: list[str] | None = None
    level_range: LevelRange | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------


class Lore(BaseModel):
    id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    region_id: str | None = Field(default=None, min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    related_npcs: list[str] | None = None
    related_factions: list[str] | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Fauna
# ---------------------------------------------------------------------------


class Fauna(BaseModel):
    id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    region_ids: list[str] = Field(min_length=1)
    creature_type: str = Field(min_length=1)
    level: int = Field(ge=1, le=20)
    hostile: bool = False
    gathering_yields: list[str] | None = None

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


class ScenarioCheck(BaseModel):
    type: str = Field(min_length=1)
    dc: int = Field(ge=1, le=30)


class ScenarioStage(BaseModel):
    stage_id: str = Field(min_length=1)
    description: str
    check: ScenarioCheck | None = None
    success_outcome: str | None = None
    failure_outcome: str | None = None


class ScenarioPrerequisites(BaseModel):
    faction_requirements: list[str] = Field(default_factory=list)
    quest_requirements: list[str] = Field(default_factory=list)
    min_relationship_with_npc: int | None = None


class ScenarioFactionChange(BaseModel):
    faction_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    amount: int = Field(ge=-100, le=100)


class ScenarioRewards(BaseModel):
    companion_unlocked: str | None = None
    faction_change: ScenarioFactionChange | None = None
    items: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    type: str = Field(min_length=1)
    companion_npc_id: str | None = None
    region_id: str = Field(min_length=1)
    level_range: LevelRange
    prerequisites: ScenarioPrerequisites
    trigger_conditions: list[str] = Field(min_length=1)
    stages: list[ScenarioStage] = Field(min_length=1)
    completion_rewards: ScenarioRewards

    model_config = {"extra": "forbid"}
