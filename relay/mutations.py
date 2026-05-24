"""Typed mutation models for state changes that lack audit trails.

Extends the TransactionLog (economy) and FactionStandingLog (factions)
pattern to combat, companions, and conditions. Each model validates the
metadata carried alongside a state change — source, reason, before/after
values — so debug visibility is guaranteed by construction.

Usage: create a mutation model at the point of change, then pass it to
``log_state_change()`` which persists a ``StateChangeLog`` row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HPChange(BaseModel):
    """Damage or healing applied to a character."""

    character_id: str
    value: int
    damage_type: str | None = None
    hp_before: int
    hp_after: int
    hp_max: int
    source: Literal[
        "combat_attack",
        "combat_save",
        "combat_save_half",
        "healing",
        "rest",
        "environment",
        "companion_ability",
    ]
    source_id: str | None = None
    reason: str

    model_config = {"extra": "forbid"}


class ConditionChange(BaseModel):
    """A condition added to or removed from a character."""

    character_id: str
    condition_id: str
    action: Literal["add", "remove", "expire"]
    duration_turns: int | None = None
    source: str
    source_id: str | None = None
    reason: str

    model_config = {"extra": "forbid"}


class ExhaustionChange(BaseModel):
    """Exhaustion level change on a character or companion."""

    character_id: str
    old_level: int
    new_level: int
    source: Literal[
        "death_state",
        "rest",
        "companion_incapacitation",
        "environment",
        "other",
    ]
    reason: str

    model_config = {"extra": "forbid"}


class DeathStateChange(BaseModel):
    """Character entering or leaving death state (0 HP)."""

    character_id: str
    entered: bool
    hp_before: int
    hp_after: int
    exhaustion_level: int
    source: str
    reason: str

    model_config = {"extra": "forbid"}


class CompanionStateChange(BaseModel):
    """State change on a companion NPC (exhaustion, loyalty, activation)."""

    character_id: str
    npc_id: str
    field: Literal["exhaustion_level", "loyalty_strain", "active", "hp_current"]
    old_value: int | bool
    new_value: int | bool
    source: Literal[
        "incapacitation",
        "dismissal",
        "recovery",
        "combat",
        "rest",
        "other",
    ]
    reason: str

    model_config = {"extra": "forbid"}


class RestEffect(BaseModel):
    """Mechanical effects of a short or long rest."""

    character_id: str
    rest_type: Literal["short", "long"]
    hp_before: int
    hp_after: int
    exhaustion_before: int
    exhaustion_after: int

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# World mutation models — proposed by the LLM via scene_analysis, validated
# and applied by the consequence evaluation logic in dialogue.py.
# ---------------------------------------------------------------------------


class FactionStandingMutation(BaseModel):
    """LLM-proposed faction standing change from a narrative event."""

    character_id: str
    faction_id: str
    delta: int
    old_value: int
    new_value: int
    reason: str
    source: Literal["consequence"] = "consequence"

    model_config = {"extra": "forbid"}


class RelationshipMutation(BaseModel):
    """LLM-proposed NPC relationship change from a narrative event."""

    character_id: str
    npc_id: str
    delta: int
    old_value: int
    new_value: int
    reason: str
    source: Literal["consequence"] = "consequence"

    model_config = {"extra": "forbid"}


class WorldFlagMutation(BaseModel):
    """LLM-proposed world flag set/change from a narrative event."""

    character_id: str
    flag: str
    value: str = "true"
    reason: str
    source: Literal["consequence"] = "consequence"

    model_config = {"extra": "forbid"}
