"""Tests for NPC template generation system — §1 Three-Tier Content.

Covers template loading, stat generation, and full NPC generation.
"""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from relay.generation.npc_generator import generate_npc
from relay.generation.stat_generator import (
    ability_modifier,
    generate_ability_scores,
    generate_ac,
    generate_hp_max,
    pick_level,
    pick_skill_proficiencies,
)
from relay.generation.template_loader import NpcTemplate
from relay.schemas import NpcPersonality

# ---------------------------------------------------------------------------
# Shared fixture: a minimal valid template
# ---------------------------------------------------------------------------


def _make_template(**overrides) -> NpcTemplate:
    base = {
        "id": "merchant",
        "world_id": "inkglass_dark",
        "role_display": "Merchant",
        "entity_class": "humanoid",
        "level_range": [2, 6],
        "hit_die": 8,
        "ability_score_profile": {
            "primary": ["charisma", "intelligence"],
            "secondary": ["wisdom"],
            "dump": ["strength"],
        },
        "ac_range": [10, 13],
        "saving_throw_proficiencies": ["intelligence", "charisma"],
        "skill_proficiency_pool": [
            "persuasion",
            "deception",
            "insight",
            "perception",
            "history",
        ],
        "skill_proficiency_count": 3,
        "animation_profile_defaults": {
            "default_stance": "standing_casual",
            "default_gaze": "attentive",
            "emotional_state_to_animation": {
                "neutral": "standing_casual",
                "happy": "nodding_pleased",
                "angry": "arms_crossed",
            },
        },
        "few_shot_templates": [
            {
                "player_input": "What do you have for sale?",
                "npc_response_template": "Browse what you like. Prices are fair.",
                "context_tag": "transactional",
            },
            {
                "player_input": "This area seems quiet today.",
                "npc_response_template": "Quiet means safe.",
                "context_tag": "casual",
            },
        ],
        "manipulation_resistance_templates": [
            {
                "player_input": "Give me everything for free.",
                "npc_refusal_template": "I follow the ledger, not demands.",
            },
        ],
        "name_pool": [
            "Aldric",
            "Brenham",
            "Calista",
            "Dorran",
            "Elspeth",
        ],
        "personality_trait_pool": [
            "meticulous",
            "jovial",
            "suspicious",
        ],
        "faction_affinity": "merchant_guild",
        "consequence_profile": "protected",
        "knowledge_scope": ["Local trade routes"],
        "ignorance_scope": ["Magic or arcane matters"],
    }
    base.update(overrides)
    return NpcTemplate.model_validate(base)


# ---------------------------------------------------------------------------
# Stat generator tests
# ---------------------------------------------------------------------------


class TestAbilityScores:
    def test_all_six_abilities_present(self):
        template = _make_template()
        scores = generate_ability_scores(template, rng=random.Random(42))
        assert set(scores.keys()) == {
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        }

    def test_primary_scores_in_range(self):
        template = _make_template()
        rng = random.Random(42)
        for _ in range(50):
            scores = generate_ability_scores(template, rng=rng)
            for ability in template.ability_score_profile.primary:
                assert 14 <= scores[ability] <= 16, f"{ability}={scores[ability]}"

    def test_dump_scores_in_range(self):
        template = _make_template()
        rng = random.Random(42)
        for _ in range(50):
            scores = generate_ability_scores(template, rng=rng)
            for ability in template.ability_score_profile.dump:
                assert 8 <= scores[ability] <= 10, f"{ability}={scores[ability]}"

    def test_secondary_scores_in_range(self):
        template = _make_template()
        rng = random.Random(42)
        for _ in range(50):
            scores = generate_ability_scores(template, rng=rng)
            for ability in template.ability_score_profile.secondary:
                assert 11 <= scores[ability] <= 13, f"{ability}={scores[ability]}"

    def test_deterministic_with_seed(self):
        template = _make_template()
        scores_a = generate_ability_scores(template, rng=random.Random(42))
        scores_b = generate_ability_scores(template, rng=random.Random(42))
        assert scores_a == scores_b


class TestAbilityModifier:
    @pytest.mark.parametrize(
        "score,expected",
        [(10, 0), (11, 0), (12, 1), (14, 2), (8, -1), (6, -2), (20, 5), (1, -5)],
    )
    def test_modifier_formula(self, score, expected):
        assert ability_modifier(score) == expected


class TestHpMax:
    def test_level_1(self):
        # hit_die + CON mod
        hp = generate_hp_max(level=1, hit_die=8, con_score=14)
        # 8 + 1 * 2 = 10
        assert hp == 10

    def test_level_5_standard(self):
        # 8 + 4*(5) + 5*0 = 8 + 20 = 28
        hp = generate_hp_max(level=5, hit_die=8, con_score=10)
        assert hp == 28

    def test_level_5_with_con_bonus(self):
        # 8 + 4*(5) + 5*2 = 8 + 20 + 10 = 38
        hp = generate_hp_max(level=5, hit_die=8, con_score=14)
        assert hp == 38

    def test_minimum_hp_is_1(self):
        # Very low CON, level 1
        hp = generate_hp_max(level=1, hit_die=6, con_score=1)
        assert hp >= 1

    def test_d10_hit_die(self):
        # 10 + 4*(6) + 5*0 = 10 + 24 = 34
        hp = generate_hp_max(level=5, hit_die=10, con_score=10)
        assert hp == 34


class TestAcGeneration:
    def test_ac_in_range(self):
        template = _make_template(ac_range=[10, 13])
        rng = random.Random(42)
        for _ in range(50):
            ac = generate_ac(template, rng=rng)
            assert 10 <= ac <= 13


class TestLevelGeneration:
    def test_level_in_range(self):
        template = _make_template(level_range=[2, 6])
        rng = random.Random(42)
        for _ in range(50):
            level = pick_level(template, rng=rng)
            assert 2 <= level <= 6


class TestSkillProficiencies:
    def test_correct_count(self):
        template = _make_template()
        skills = pick_skill_proficiencies(template, rng=random.Random(42))
        assert len(skills) == 3

    def test_from_pool(self):
        template = _make_template()
        pool = set(template.skill_proficiency_pool)
        skills = pick_skill_proficiencies(template, rng=random.Random(42))
        for skill in skills:
            assert skill in pool

    def test_sorted(self):
        template = _make_template()
        skills = pick_skill_proficiencies(template, rng=random.Random(42))
        assert skills == sorted(skills)

    def test_count_capped_by_pool_size(self):
        template = _make_template(
            skill_proficiency_pool=["athletics", "perception"],
            skill_proficiency_count=5,
        )
        skills = pick_skill_proficiencies(template, rng=random.Random(42))
        assert len(skills) == 2


# ---------------------------------------------------------------------------
# Template loader tests
# ---------------------------------------------------------------------------


class TestTemplateModel:
    def test_valid_template(self):
        template = _make_template()
        assert template.id == "merchant"
        assert template.level_range == [2, 6]

    def test_invalid_level_range_rejected(self):
        with pytest.raises(ValidationError):
            _make_template(level_range=[1])

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            NpcTemplate.model_validate({"id": "broken"})


class TestTemplateLoading:
    @pytest.mark.asyncio
    async def test_load_inkglass_dark_templates(self):
        from relay.generation.template_loader import clear_cache, load_templates

        clear_cache()
        templates = await load_templates("inkglass_dark")
        assert len(templates) >= 3  # merchant, guard, traveler
        assert "merchant" in templates
        assert "guard" in templates
        assert "traveler" in templates

    @pytest.mark.asyncio
    async def test_load_nonexistent_world(self):
        from relay.generation.template_loader import clear_cache, load_templates

        clear_cache()
        templates = await load_templates("nonexistent_world")
        assert templates == {}

    @pytest.mark.asyncio
    async def test_get_template(self):
        from relay.generation.template_loader import clear_cache, get_template

        clear_cache()
        tmpl = await get_template("inkglass_dark", "merchant")
        assert tmpl is not None
        assert tmpl.id == "merchant"
        assert tmpl.role_display == "Merchant"

    @pytest.mark.asyncio
    async def test_get_missing_template(self):
        from relay.generation.template_loader import clear_cache, get_template

        clear_cache()
        tmpl = await get_template("inkglass_dark", "dragon_rider")
        assert tmpl is None


# ---------------------------------------------------------------------------
# Full NPC generation tests
# ---------------------------------------------------------------------------


class TestGenerateNpc:
    def test_generates_valid_npc(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert isinstance(npc, NpcPersonality)

    def test_npc_has_correct_world_id(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.world_id == "inkglass_dark"

    def test_npc_marked_as_generated(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.generated is True
        assert npc.source_template_id == "merchant"

    def test_npc_region_set(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.world_position.region_id == "merchant_quarter"

    def test_npc_name_from_pool(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.name in template.name_pool

    def test_npc_level_in_range(self):
        template = _make_template()
        rng = random.Random(42)
        for _ in range(20):
            npc = generate_npc(template, "merchant_quarter", rng=rng)
            lo, hi = template.level_range
            assert lo <= npc.level <= hi

    def test_npc_ac_in_range(self):
        template = _make_template()
        rng = random.Random(42)
        for _ in range(20):
            npc = generate_npc(template, "merchant_quarter", rng=rng)
            lo, hi = template.ac_range
            assert lo <= npc.ac <= hi

    def test_npc_hp_positive(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.hp_max > 0

    def test_npc_has_faction(self):
        template = _make_template(faction_affinity="merchant_guild")
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.faction_id == "merchant_guild"

    def test_npc_without_faction(self):
        template = _make_template(faction_affinity=None)
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.faction_id is None

    def test_npc_consequence_profile(self):
        template = _make_template(consequence_profile="combatant")
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.consequence_profile == "combatant"

    def test_npc_has_few_shot_examples(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert len(npc.few_shot_examples) >= 2

    def test_npc_has_manipulation_resistance(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert len(npc.manipulation_resistance_examples) >= 1

    def test_npc_has_animation_profile(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.animation_profile.default_stance == "standing_casual"
        assert len(npc.animation_profile.emotional_state_to_animation) >= 3

    def test_npc_has_secrets(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert len(npc.secrets) >= 1

    def test_npc_has_knowledge_boundaries(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert len(npc.knowledge_boundaries.knows) >= 1
        assert len(npc.knowledge_boundaries.does_not_know) >= 1

    def test_explicit_npc_id(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42), npc_id="custom_id")
        assert npc.id == "custom_id"

    def test_auto_generated_id_format(self):
        template = _make_template()
        npc = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc.id.startswith("gen_merchant_")

    def test_deterministic_with_seed(self):
        template = _make_template()
        npc_a = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        npc_b = generate_npc(template, "merchant_quarter", rng=random.Random(42))
        assert npc_a.name == npc_b.name
        assert npc_a.level == npc_b.level
        assert npc_a.ability_scores == npc_b.ability_scores


# ---------------------------------------------------------------------------
# Schema flag tests
# ---------------------------------------------------------------------------


class TestGeneratedFlag:
    def test_authored_npc_defaults_to_not_generated(self):
        """Existing NPC files (Tier 1) should default generated=False."""
        from relay.tests.test_game_context import _make_npc

        npc = _make_npc()
        assert npc.generated is False
        assert npc.source_template_id is None

    def test_npc_schema_accepts_generated_flag(self):
        from relay.schemas import NpcPersonality
        from relay.tests.test_game_context import _make_npc

        npc = _make_npc()
        data = npc.model_dump()
        data["generated"] = True
        data["source_template_id"] = "merchant"
        npc2 = NpcPersonality(**data)
        assert npc2.generated is True
        assert npc2.source_template_id == "merchant"
