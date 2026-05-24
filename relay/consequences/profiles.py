"""NPC consequence profiles — tag-based rules for violence consequences.

Four tags determine how the consequence system treats violence against an NPC:

  protected  — Full consequences (faction hit, relationship hit, wanted flags,
               director escalation).  Default for all NPCs.
  combatant  — No faction/crime consequences.  Death tracked as "defeated".
  hostile    — No consequences.  They attacked first.  Death tracked as "killed".
  ephemeral  — No tracking at all.  Background/filler NPCs.

The relay reads the tag and applies rules.  The LLM never decides whether
killing was justified — it writes the narrative; the system decides mechanical
consequences.  Invariant #8.

Scenario context overrides are supported: an NPC who is normally "protected"
can be treated as "combatant" within a tournament scenario.  Override lookup
is a TODO until scenarios are implemented (Phase 2).

Design doc: docs/design_proposals.md §8 (NPC Consequence Tags)
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

ConsequenceProfile = Literal["protected", "combatant", "hostile", "ephemeral"]

VALID_PROFILES: frozenset[str] = frozenset({"protected", "combatant", "hostile", "ephemeral"})

# Map profile → death_handling value written to NPC instance state.
_DEATH_HANDLING: dict[str, str] = {
    "protected": "dead",
    "combatant": "defeated",
    "hostile": "dead",
    "ephemeral": "dead",  # never written — ephemeral NPCs aren't tracked
}


def resolve_profile(
    npc_profile: str | None,
    *,
    scenario_override: str | None = None,
) -> ConsequenceProfile:
    """Resolve the effective consequence profile for an NPC.

    Parameters
    ----------
    npc_profile
        The NPC's ``consequence_profile`` field from their personality file.
        Defaults to "protected" if missing or invalid.
    scenario_override
        If the active scenario overrides this NPC's profile (e.g. tournament
        treats all contestants as "combatant"), this takes precedence.
        TODO: scenario override lookup is Phase 2.

    Returns
    -------
    ConsequenceProfile
        The effective profile for consequence evaluation.
    """
    if scenario_override and scenario_override in VALID_PROFILES:
        return scenario_override  # type: ignore[return-value]

    if npc_profile and npc_profile in VALID_PROFILES:
        return npc_profile  # type: ignore[return-value]

    return "protected"


def get_death_handling(profile: ConsequenceProfile) -> str:
    """Return the death_handling value for a profile.

    "protected" and "hostile" NPCs are marked "dead".
    "combatant" NPCs are marked "defeated" (they can return).
    "ephemeral" NPCs are never tracked, but return "dead" as a fallback.
    """
    return _DEATH_HANDLING.get(profile, "dead")


def should_apply_faction_consequences(profile: ConsequenceProfile) -> bool:
    """Whether violence should trigger faction standing and crime flags."""
    return profile == "protected"


def should_apply_relationship_consequences(profile: ConsequenceProfile) -> bool:
    """Whether violence should trigger relationship changes."""
    return profile == "protected"


def should_track_instance_state(profile: ConsequenceProfile) -> bool:
    """Whether the NPC's instance state should be updated."""
    return profile != "ephemeral"


def filter_mutations_by_profile(
    mutations: list[dict],
    profile: ConsequenceProfile,
) -> list[dict]:
    """Filter world mutations based on the NPC's consequence profile.

    For "protected" NPCs, all mutations pass through.
    For "combatant" NPCs, faction and crime-flag mutations are removed.
    For "hostile" NPCs, all consequence mutations are removed.
    For "ephemeral" NPCs, all mutations are removed.

    World flags that are NOT crime/faction related (e.g. quest flags set by
    the narrative) always pass through regardless of profile.
    """
    if profile == "protected":
        return mutations

    if profile == "ephemeral":
        return []

    filtered: list[dict] = []
    for m in mutations:
        mut_type = m.get("type")

        if profile == "hostile":
            # Hostile: no consequences at all — only world flags that aren't
            # crime-related pass through
            if mut_type == "world_flag_set":
                flag = m.get("flag", "")
                if not _is_crime_flag(flag):
                    filtered.append(m)
            continue

        if profile == "combatant":
            # Combatant: no faction or crime consequences, relationships still apply
            if mut_type == "faction_standing_change":
                continue
            if mut_type == "world_flag_set":
                flag = m.get("flag", "")
                if _is_crime_flag(flag):
                    continue
            filtered.append(m)

    return filtered


def _is_crime_flag(flag: str) -> bool:
    """Heuristic: flags prefixed with 'wanted_in:' or 'bounty' are crime flags."""
    return flag.startswith("wanted_in:") or flag.startswith("bounty")
