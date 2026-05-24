"""Tests for NPC consequence profiles — tag-based violence consequence rules.

Covers relay/consequences/profiles.py and the consequence_profile field
in the NPC personality schema.
"""

from __future__ import annotations

from relay.consequences.profiles import (
    VALID_PROFILES,
    filter_mutations_by_profile,
    get_death_handling,
    resolve_profile,
    should_apply_faction_consequences,
    should_apply_relationship_consequences,
    should_track_instance_state,
)

# ---------------------------------------------------------------------------
# resolve_profile tests
# ---------------------------------------------------------------------------


class TestResolveProfile:
    def test_default_is_protected(self):
        assert resolve_profile(None) == "protected"

    def test_explicit_protected(self):
        assert resolve_profile("protected") == "protected"

    def test_combatant(self):
        assert resolve_profile("combatant") == "combatant"

    def test_hostile(self):
        assert resolve_profile("hostile") == "hostile"

    def test_ephemeral(self):
        assert resolve_profile("ephemeral") == "ephemeral"

    def test_invalid_falls_back_to_protected(self):
        assert resolve_profile("invalid_tag") == "protected"

    def test_empty_string_falls_back_to_protected(self):
        assert resolve_profile("") == "protected"

    def test_scenario_override_takes_precedence(self):
        assert resolve_profile("protected", scenario_override="combatant") == "combatant"

    def test_scenario_override_invalid_ignored(self):
        assert resolve_profile("hostile", scenario_override="invalid") == "hostile"

    def test_scenario_override_none_uses_npc_profile(self):
        assert resolve_profile("combatant", scenario_override=None) == "combatant"


# ---------------------------------------------------------------------------
# Death handling tests
# ---------------------------------------------------------------------------


class TestDeathHandling:
    def test_protected_dies(self):
        assert get_death_handling("protected") == "dead"

    def test_combatant_defeated(self):
        assert get_death_handling("combatant") == "defeated"

    def test_hostile_dies(self):
        assert get_death_handling("hostile") == "dead"

    def test_ephemeral_dies(self):
        assert get_death_handling("ephemeral") == "dead"


# ---------------------------------------------------------------------------
# Boolean flag tests
# ---------------------------------------------------------------------------


class TestConsequenceFlags:
    def test_protected_gets_faction_consequences(self):
        assert should_apply_faction_consequences("protected") is True

    def test_combatant_no_faction_consequences(self):
        assert should_apply_faction_consequences("combatant") is False

    def test_hostile_no_faction_consequences(self):
        assert should_apply_faction_consequences("hostile") is False

    def test_ephemeral_no_faction_consequences(self):
        assert should_apply_faction_consequences("ephemeral") is False

    def test_protected_gets_relationship_consequences(self):
        assert should_apply_relationship_consequences("protected") is True

    def test_combatant_no_relationship_consequences(self):
        assert should_apply_relationship_consequences("combatant") is False

    def test_protected_tracked(self):
        assert should_track_instance_state("protected") is True

    def test_combatant_tracked(self):
        assert should_track_instance_state("combatant") is True

    def test_hostile_tracked(self):
        assert should_track_instance_state("hostile") is True

    def test_ephemeral_not_tracked(self):
        assert should_track_instance_state("ephemeral") is False


# ---------------------------------------------------------------------------
# Mutation filter tests
# ---------------------------------------------------------------------------


class TestFilterMutationsByProfile:
    def _faction_mutation(self, faction_id="guild", delta=-10):
        return {
            "type": "faction_standing_change",
            "faction_id": faction_id,
            "delta": delta,
            "reason": "violence",
        }

    def _relationship_mutation(self, npc_id="guard", delta=-20):
        return {
            "type": "relationship_change",
            "npc_id": npc_id,
            "delta": delta,
            "reason": "attack",
        }

    def _crime_flag_mutation(self, flag="wanted_in:market_district"):
        return {"type": "world_flag_set", "flag": flag, "reason": "witnessed crime"}

    def _quest_flag_mutation(self, flag="quest_completed:rescue"):
        return {"type": "world_flag_set", "flag": flag, "reason": "quest"}

    # --- Protected: everything passes ---

    def test_protected_passes_all(self):
        mutations = [
            self._faction_mutation(),
            self._relationship_mutation(),
            self._crime_flag_mutation(),
            self._quest_flag_mutation(),
        ]
        result = filter_mutations_by_profile(mutations, "protected")
        assert len(result) == 4

    # --- Combatant: no faction or crime flags ---

    def test_combatant_blocks_faction(self):
        mutations = [self._faction_mutation()]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 0

    def test_combatant_keeps_relationship(self):
        mutations = [self._relationship_mutation()]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 1

    def test_combatant_blocks_crime_flag(self):
        mutations = [self._crime_flag_mutation()]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 0

    def test_combatant_blocks_bounty_flag(self):
        mutations = [self._crime_flag_mutation("bounty_active")]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 0

    def test_combatant_keeps_quest_flag(self):
        mutations = [self._quest_flag_mutation()]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 1

    def test_combatant_mixed(self):
        mutations = [
            self._faction_mutation(),
            self._relationship_mutation(),
            self._crime_flag_mutation(),
            self._quest_flag_mutation(),
        ]
        result = filter_mutations_by_profile(mutations, "combatant")
        assert len(result) == 2
        types = [m["type"] for m in result]
        assert "relationship_change" in types
        assert "world_flag_set" in types
        assert result[1]["flag"] == "quest_completed:rescue"

    # --- Hostile: no consequences except non-crime flags ---

    def test_hostile_blocks_faction(self):
        mutations = [self._faction_mutation()]
        result = filter_mutations_by_profile(mutations, "hostile")
        assert len(result) == 0

    def test_hostile_blocks_relationship(self):
        mutations = [self._relationship_mutation()]
        result = filter_mutations_by_profile(mutations, "hostile")
        assert len(result) == 0

    def test_hostile_blocks_crime_flag(self):
        mutations = [self._crime_flag_mutation()]
        result = filter_mutations_by_profile(mutations, "hostile")
        assert len(result) == 0

    def test_hostile_keeps_quest_flag(self):
        mutations = [self._quest_flag_mutation()]
        result = filter_mutations_by_profile(mutations, "hostile")
        assert len(result) == 1

    # --- Ephemeral: everything blocked ---

    def test_ephemeral_blocks_all(self):
        mutations = [
            self._faction_mutation(),
            self._relationship_mutation(),
            self._crime_flag_mutation(),
            self._quest_flag_mutation(),
        ]
        result = filter_mutations_by_profile(mutations, "ephemeral")
        assert len(result) == 0

    # --- Edge cases ---

    def test_empty_mutations_returns_empty(self):
        for profile in VALID_PROFILES:
            assert filter_mutations_by_profile([], profile) == []


# ---------------------------------------------------------------------------
# Schema integration test
# ---------------------------------------------------------------------------


class TestNpcPersonalityProfile:
    def test_default_profile_is_protected(self):
        """NPC without consequence_profile defaults to protected."""
        from relay.tests.test_game_context import _make_npc

        npc = _make_npc()
        assert npc.consequence_profile == "protected"

    def test_explicit_combatant(self):
        from relay.tests.test_game_context import _make_npc

        npc = _make_npc()
        # Override for test
        npc_data = npc.model_dump()
        npc_data["consequence_profile"] = "combatant"
        from relay.schemas import NpcPersonality

        npc2 = NpcPersonality(**npc_data)
        assert npc2.consequence_profile == "combatant"

    def test_invalid_profile_rejected(self):
        import pytest
        from pydantic import ValidationError

        from relay.schemas import NpcPersonality
        from relay.tests.test_game_context import _make_npc

        npc_data = _make_npc().model_dump()
        npc_data["consequence_profile"] = "invalid_tag"
        with pytest.raises(ValidationError):
            NpcPersonality(**npc_data)
