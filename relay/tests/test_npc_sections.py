"""Tests for NPC character sections in prompts and relay-decided secret unlocking."""

from __future__ import annotations

import json
from pathlib import Path

from relay.ai.chat_prompts import build_quickchat_system_prompt
from relay.ai.npc_sections import format_character_sections, unlocked_secrets
from relay.ai.rp_prompts import build_final_prose_messages, build_rp_system_prompt
from relay.schemas import NpcKnowledgeBoundaries, NpcPersonality, NpcSecret
from relay.tests.test_dialogue import _make_npc

_SCHEMA = json.loads((Path(__file__).parents[2] / "schemas" / "npc_personality.json").read_text(encoding="utf-8"))


def _npc(**overrides) -> NpcPersonality:
    return _make_npc().model_copy(update=overrides)


def _secret(**kw) -> NpcSecret:
    return NpcSecret(content=kw.pop("content", "The hat is fake."), secret_type="identity", **kw)


class TestCharacterSections:
    def test_new_fields_appear_when_set(self) -> None:
        npc = _npc(
            appearance="Tall, ink-stained cuffs.",
            cover_story="Claims to be a pointed hat healer.",
            style_rules=["No em dashes"],
            knowledge_boundaries=NpcKnowledgeBoundaries(
                knows=["herbs"], does_not_know=["politics"], believes_wrongly=["The Knights never enter the lanes"]
            ),
        )
        text = format_character_sections(npc)
        assert "APPEARANCE\nTall, ink-stained cuffs." in text
        assert "PUBLIC FACE" in text and "Claims to be a pointed hat healer." in text
        assert "STYLE RULES FOR YOUR PROSE\n  - No em dashes" in text
        assert "The Knights never enter the lanes" in text
        assert "ABILITIES\nKnowledgeable about plants." in text
        assert "npc_friend (ally): Old friend" in text

    def test_unset_fields_are_omitted(self) -> None:
        text = format_character_sections(_npc())
        assert "APPEARANCE" not in text
        assert "PUBLIC FACE" not in text
        assert "STYLE RULES" not in text
        assert "BELIEFS" not in text

    def test_secrets_are_listed_with_the_guard_instruction(self) -> None:
        text = format_character_sections(_npc(secrets=[_secret(reveal_condition="never")]))
        assert "SECRETS\nYou are hiding these. Never volunteer them" in text
        assert "Who you really are: The hat is fake." in text

    def test_both_prompts_include_the_sections(self) -> None:
        npc = _npc(appearance="Tall, ink-stained cuffs.")
        rp = build_rp_system_prompt(npc)[0]["text"]
        chat = build_quickchat_system_prompt(npc)
        for prompt in (rp, chat):
            assert "Tall, ink-stained cuffs." in prompt
            assert "Hides a rare seed." in prompt
            # Sections sit before the rules so the rules have the last word.
            assert prompt.index("SECRETS") < prompt.index("RULES")


class TestUnlockedSecrets:
    def _check(self, **kw) -> dict:
        return {"skill": "insight", "passed": True, "total": 18, **kw}

    def test_passed_check_reaching_the_dc_unlocks(self) -> None:
        npc = _npc(
            secrets=[_secret(reveal_condition="check_type_and_dc", reveal_check_type="Insight", reveal_check_dc=16)]
        )
        assert unlocked_secrets(npc, [self._check()]) == ["The hat is fake."]

    def test_check_below_the_dc_does_not_unlock(self) -> None:
        npc = _npc(
            secrets=[_secret(reveal_condition="check_type_and_dc", reveal_check_type="insight", reveal_check_dc=16)]
        )
        assert unlocked_secrets(npc, [self._check(total=15)]) == []

    def test_failed_or_wrong_skill_check_does_not_unlock(self) -> None:
        npc = _npc(
            secrets=[_secret(reveal_condition="check_type_and_dc", reveal_check_type="insight", reveal_check_dc=10)]
        )
        assert unlocked_secrets(npc, [self._check(passed=False)]) == []
        assert unlocked_secrets(npc, [self._check(skill="perception")]) == []

    def test_relationship_threshold(self) -> None:
        npc = _npc(secrets=[_secret(reveal_condition="relationship_threshold", reveal_threshold=25)])
        assert unlocked_secrets(npc, relationship_score=24) == []
        assert unlocked_secrets(npc, relationship_score=25) == ["The hat is fake."]

    def test_quest_flag(self) -> None:
        npc = _npc(secrets=[_secret(reveal_condition="quest_flag", reveal_quest_flag="saw_the_grove")])
        assert unlocked_secrets(npc, flags=["other"]) == []
        assert unlocked_secrets(npc, flags=["saw_the_grove"]) == ["The hat is fake."]

    def test_never_is_never_unlocked(self) -> None:
        npc = _npc(secrets=[_secret(reveal_condition="never")])
        assert unlocked_secrets(npc, [self._check(total=30)], relationship_score=100, flags=["x"]) == []


class TestFinalProseUnlock:
    def test_unlocked_secret_reaches_the_final_call(self) -> None:
        msgs = build_final_prose_messages("Who are you really?", "draft", [], [], unlocked_secrets=["The hat is fake."])
        assert "UNLOCKED THIS TURN" in msgs[-1]["content"]
        assert "The hat is fake." in msgs[-1]["content"]

    def test_nothing_unlocked_adds_nothing(self) -> None:
        msgs = build_final_prose_messages("Who are you really?", "draft", [], [])
        assert "UNLOCKED" not in msgs[-1]["content"]


class TestSchemaMirror:
    """The JSON schema and the Pydantic model must list the same fields (CLAUDE.md §6)."""

    def test_npc_fields_match(self) -> None:
        assert set(_SCHEMA["properties"]) == set(NpcPersonality.model_fields)

    def test_knowledge_boundary_fields_match(self) -> None:
        props = _SCHEMA["properties"]["knowledge_boundaries"]["properties"]
        assert set(props) == set(NpcKnowledgeBoundaries.model_fields)
