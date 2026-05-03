from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared sub-models
# ---------------------------------------------------------------------------

class ConditionEntry(BaseModel):
    condition_id: str
    duration_turns: int | None = None
    expiry_turn: int | None = None
    source: str


class InventoryEntry(BaseModel):
    item_id: str
    quantity: int = Field(ge=1)
    binding_state: Literal["unbound", "bound"]


class CompanionEntry(BaseModel):
    npc_id: str
    behavior_type: Literal["aggressive", "supportive", "defensive"]
    loyalty_strain: int = Field(ge=0)


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


class ShopData(BaseModel):
    inventory: list[ShopInventoryEntry]
    pricing_policy: str
    restock_schedule: Literal["daily", "weekly", "never"]
    access_prerequisites: dict | None = None


class CompanionRecruitment(BaseModel):
    affection_threshold: int
    recruitment_scenario_id: str
    recruitment_conditions: list[str] | None = None


class CompanionCombatProfile(BaseModel):
    behavior_type: Literal["aggressive", "supportive", "defensive"]
    abilities: list[str] = Field(min_length=2, max_length=3)
    directive_vocabulary: dict[str, str] | None = None


class CompanionAmbientBehavior(BaseModel):
    comment_frequency: str
    trigger_categories: list[str]
    mood_modifier: float | None = None


class CompanionData(BaseModel):
    recruitment: CompanionRecruitment
    combat_profile: CompanionCombatProfile
    ambient_behavior: CompanionAmbientBehavior
    loyalty_strain_threshold: int
    world_event_reactions: list[dict] | None = None
    farewell_template: str | None = None
    reunion_template: str | None = None
    dismissal_relationship_modifier: int | None = None


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

    # Conditional sections
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
    world: str
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
    world: str
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
