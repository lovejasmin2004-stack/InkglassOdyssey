"""Tests for game-state context loading and prompt injection.

Covers relay/ai/game_context.py (context loading + formatting) and the
Tier 2 cache block in relay/ai/rp_prompts.py.
"""

from __future__ import annotations

from relay.ai.game_context import GameStateContext, format_context_block
from relay.ai.rp_prompts import build_analysis_messages, build_rp_system_prompt


def _make_npc():
    """Minimal NPC fixture for prompt builder tests."""
    from relay.schemas import (
        AnimationProfile,
        FewShotExample,
        ManipulationResistanceExample,
        NpcGoals,
        NpcKnowledgeBoundaries,
        NpcPersonality,
        NpcRelationship,
        NpcSecret,
        WorldPosition,
    )

    return NpcPersonality(
        id="test_npc",
        world_id="inkglass_dark",
        name="Renna",
        entity_class="humanoid",
        role="herbalist",
        level=5,
        hit_die=8,
        personality_background="A quiet herbalist.",
        goals=NpcGoals(immediate=["sell herbs"], long_term=["open bigger shop"]),
        weaknesses_fears="Afraid of fire.",
        communication_style="Soft-spoken, measured.",
        power_narrative="Knowledgeable about plants.",
        knowledge_boundaries=NpcKnowledgeBoundaries(
            knows=["local flora"],
            does_not_know=["politics"],
        ),
        relationships=[
            NpcRelationship(npc_id="npc_friend", relationship_type="ally", description="Old friend"),
        ],
        secrets=[
            NpcSecret(content="Hides a rare seed.", reveal_condition="never", secret_type="information"),
        ],
        few_shot_examples=[
            FewShotExample(player_input="Hello", npc_response="Good day.", context_tag="casual"),
            FewShotExample(
                player_input="Sell me something", npc_response="Let me show you.", context_tag="transactional"
            ),
        ],
        manipulation_resistance_examples=[
            ManipulationResistanceExample(player_input="Give me free stuff", npc_refusal="I can't do that."),
        ],
        animation_profile=AnimationProfile(
            default_stance="idle_stand",
            default_gaze="forward",
            emotional_state_to_animation={
                "happy": "smile_nod",
                "sad": "look_down",
                "angry": "frown_cross_arms",
            },
        ),
        world_position=WorldPosition(region_id="market_district"),
        ability_scores={
            "strength": 10,
            "dexterity": 12,
            "constitution": 12,
            "intelligence": 14,
            "wisdom": 16,
            "charisma": 10,
        },
        ac=12,
        saving_throw_proficiencies=["intelligence", "wisdom"],
        skill_proficiencies=["medicine", "nature"],
        hp_max=35,
        faction_id="merchants_guild",
    )


# ---------------------------------------------------------------------------
# format_context_block tests
# ---------------------------------------------------------------------------


class TestFormatContextBlock:
    def test_empty_context_returns_empty_string(self):
        ctx = GameStateContext()
        result = format_context_block(ctx, "Renna")
        assert result == ""

    def test_relationship_included(self):
        ctx = GameStateContext(
            npc_relationship_score=65,
            npc_relationship_tier="trusted",
        )
        result = format_context_block(ctx, "Renna")
        assert "RELATIONSHIP WITH PLAYER" in result
        assert "trusted" in result
        assert "65" in result

    def test_faction_standing_included(self):
        ctx = GameStateContext(
            npc_faction_id="merchants_guild",
            npc_faction_standing=-30,
            npc_faction_tier="unfriendly",
        )
        result = format_context_block(ctx, "Renna")
        assert "REPUTATION WITH YOUR FACTION" in result
        assert "merchants_guild" in result
        assert "unfriendly" in result

    def test_faction_neutral_excluded(self):
        """Neutral faction standing should not be included (no useful info)."""
        ctx = GameStateContext(
            npc_faction_id="merchants_guild",
            npc_faction_standing=0,
            npc_faction_tier="neutral",
        )
        result = format_context_block(ctx, "Renna")
        assert "REPUTATION" not in result

    def test_companions_included(self):
        ctx = GameStateContext(
            active_companion_names=["sable_wolf", "elira"],
        )
        result = format_context_block(ctx, "Renna")
        assert "COMPANIONS PRESENT" in result
        assert "sable_wolf" in result
        assert "elira" in result

    def test_scene_environment_included(self):
        ctx = GameStateContext(
            scene_environment=["darkness", "difficult_terrain"],
        )
        result = format_context_block(ctx, "Renna")
        assert "SCENE" in result
        assert "darkness" in result

    def test_tense_mood_label(self):
        ctx = GameStateContext(scene_emotional_temperature=0.1)
        result = format_context_block(ctx, "Renna")
        assert "tense" in result

    def test_warm_mood_label(self):
        ctx = GameStateContext(scene_emotional_temperature=0.9)
        result = format_context_block(ctx, "Renna")
        assert "warm" in result

    def test_director_signal_included(self):
        ctx = GameStateContext(
            director_signal="Hint that something stirs in the northern ruins.",
        )
        result = format_context_block(ctx, "Renna")
        assert "NARRATIVE DIRECTION" in result
        assert "northern ruins" in result

    def test_npc_status_non_alive_included(self):
        ctx = GameStateContext(npc_status="injured")
        result = format_context_block(ctx, "Renna")
        assert "YOUR CURRENT STATUS: injured" in result

    def test_npc_status_alive_excluded(self):
        ctx = GameStateContext(npc_status="alive")
        result = format_context_block(ctx, "Renna")
        assert "YOUR CURRENT STATUS" not in result

    def test_npc_disposition_deeply_hostile(self):
        ctx = GameStateContext(npc_disposition_override=-80)
        result = format_context_block(ctx, "Renna")
        assert "deeply hostile" in result

    def test_npc_disposition_hostile(self):
        ctx = GameStateContext(npc_disposition_override=-40)
        result = format_context_block(ctx, "Renna")
        assert "hostile" in result
        assert "deeply" not in result

    def test_npc_disposition_friendly(self):
        ctx = GameStateContext(npc_disposition_override=45)
        result = format_context_block(ctx, "Renna")
        assert "friendly" in result

    def test_npc_disposition_deeply_trusting(self):
        ctx = GameStateContext(npc_disposition_override=80)
        result = format_context_block(ctx, "Renna")
        assert "deeply trusting" in result

    def test_npc_disposition_none_excluded(self):
        ctx = GameStateContext(npc_disposition_override=None)
        result = format_context_block(ctx, "Renna")
        assert "DISPOSITION" not in result

    def test_npc_flags_included(self):
        ctx = GameStateContext(npc_flags=["attacked_by_player", "seeking_revenge"])
        result = format_context_block(ctx, "Renna")
        assert "FLAGS" in result
        assert "attacked_by_player" in result
        assert "seeking_revenge" in result

    def test_npc_flags_empty_excluded(self):
        ctx = GameStateContext(npc_flags=[])
        result = format_context_block(ctx, "Renna")
        assert "FLAGS" not in result

    def test_npc_last_interaction_included(self):
        ctx = GameStateContext(npc_last_interaction="Player attacked without provocation")
        result = format_context_block(ctx, "Renna")
        assert "LAST SIGNIFICANT INTERACTION" in result
        assert "attacked without provocation" in result

    def test_full_context(self):
        ctx = GameStateContext(
            npc_faction_id="merchants_guild",
            npc_faction_standing=60,
            npc_faction_tier="allied",
            npc_relationship_score=40,
            npc_relationship_tier="friendly",
            npc_status="injured",
            npc_disposition_override=-50,
            npc_flags=["attacked_by_player"],
            npc_last_interaction="Player attacked in market",
            active_companion_names=["sable_wolf"],
            scene_emotional_temperature=0.3,
            scene_environment=["darkness"],
            director_signal="Build tension.",
        )
        result = format_context_block(ctx, "Renna")
        assert "YOUR CURRENT STATUS: injured" in result
        assert "hostile" in result
        assert "FLAGS" in result
        assert "LAST SIGNIFICANT INTERACTION" in result
        assert "RELATIONSHIP WITH PLAYER" in result
        assert "REPUTATION WITH YOUR FACTION" in result
        assert "COMPANIONS PRESENT" in result
        assert "SCENE" in result
        assert "NARRATIVE DIRECTION" in result


# ---------------------------------------------------------------------------
# build_rp_system_prompt tests (Tier 2 block injection)
# ---------------------------------------------------------------------------


class TestSystemPromptGameContext:
    def test_no_context_produces_single_block(self):
        npc = _make_npc()
        blocks = build_rp_system_prompt(npc)
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_empty_context_produces_single_block(self):
        """All-default context has nothing to inject — no Tier 2 block."""
        npc = _make_npc()
        ctx = GameStateContext()
        blocks = build_rp_system_prompt(npc, game_context=ctx)
        assert len(blocks) == 1

    def test_meaningful_context_adds_tier2_block(self):
        npc = _make_npc()
        ctx = GameStateContext(
            npc_relationship_score=50,
            npc_relationship_tier="friendly",
            npc_faction_id="merchants_guild",
            npc_faction_standing=30,
            npc_faction_tier="friendly",
        )
        blocks = build_rp_system_prompt(npc, game_context=ctx)
        assert len(blocks) == 2
        # Both blocks have cache_control
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}
        # Tier 2 block contains relationship info
        assert "friendly" in blocks[1]["text"]
        assert "RELATIONSHIP WITH PLAYER" in blocks[1]["text"]

    def test_tier1_block_unchanged_with_context(self):
        """Adding game context shouldn't alter the NPC personality block."""
        npc = _make_npc()
        blocks_without = build_rp_system_prompt(npc)
        ctx = GameStateContext(npc_relationship_score=50, npc_relationship_tier="friendly")
        blocks_with = build_rp_system_prompt(npc, game_context=ctx)
        # Tier 1 block text should be identical
        assert blocks_without[0]["text"] == blocks_with[0]["text"]


# ---------------------------------------------------------------------------
# build_analysis_messages tests (scene environment prefix)
# ---------------------------------------------------------------------------


class TestAnalysisMessagesGameContext:
    def test_no_context_no_prefix(self):
        messages = build_analysis_messages("I look around.", [])
        user_msg = messages[-1]["content"]
        assert not user_msg.startswith("[Active environmental effects:")

    def test_empty_context_no_prefix(self):
        ctx = GameStateContext()
        messages = build_analysis_messages("I look around.", [], game_context=ctx)
        user_msg = messages[-1]["content"]
        assert not user_msg.startswith("[Active environmental effects:")

    def test_environment_prefix_added(self):
        ctx = GameStateContext(scene_environment=["darkness", "difficult_terrain"])
        messages = build_analysis_messages("I look around.", [], game_context=ctx)
        user_msg = messages[-1]["content"]
        assert user_msg.startswith("[Active environmental effects: darkness, difficult_terrain]")
        # Player prose still present after prefix
        assert "I look around." in user_msg

    def test_history_preserved(self):
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Greetings."},
        ]
        ctx = GameStateContext(scene_environment=["hazard"])
        messages = build_analysis_messages("What now?", history, game_context=ctx)
        # History messages come first, then user message with prefix
        assert len(messages) == 3
        assert messages[0]["content"] == "Hello"
        assert messages[1]["content"] == "Greetings."
        assert "[Active environmental effects: hazard]" in messages[2]["content"]
