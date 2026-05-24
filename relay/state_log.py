"""Helpers for writing StateChangeLog entries from typed mutation models.

Mirrors the pattern of ``relay.economy.wallet.log_item_transaction`` —
callers create a mutation model, then call the appropriate log function.
The log entry is added to the session but NOT committed; the caller
controls transaction boundaries.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import StateChangeLog
from relay.mutations import (
    CompanionStateChange,
    ConditionChange,
    DeathStateChange,
    ExhaustionChange,
    FactionStandingMutation,
    HPChange,
    RelationshipMutation,
    RestEffect,
    WorldFlagMutation,
)


def _id() -> str:
    return str(uuid.uuid4())


async def log_hp_change(db: AsyncSession, m: HPChange, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="hp_change",
            source=m.source,
            source_id=m.source_id,
            reason=m.reason,
            delta=m.value,
            old_value=m.hp_before,
            new_value=m.hp_after,
            damage_type=m.damage_type,
        )
    )


async def log_condition_change(db: AsyncSession, m: ConditionChange, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type=f"condition_{m.action}",
            source=m.source,
            source_id=m.source_id,
            reason=m.reason,
            condition_id=m.condition_id,
        )
    )


async def log_exhaustion_change(db: AsyncSession, m: ExhaustionChange, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="exhaustion_change",
            source=m.source,
            reason=m.reason,
            delta=m.new_level - m.old_level,
            old_value=m.old_level,
            new_value=m.new_level,
        )
    )


async def log_death_state(db: AsyncSession, m: DeathStateChange, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="death_state",
            source=m.source,
            reason=m.reason,
            old_value=m.hp_before,
            new_value=m.hp_after,
        )
    )


async def log_companion_state(db: AsyncSession, m: CompanionStateChange, *, session_id: str | None = None) -> None:
    old_int = int(m.old_value) if isinstance(m.old_value, bool) else m.old_value
    new_int = int(m.new_value) if isinstance(m.new_value, bool) else m.new_value
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="companion_state",
            source=m.source,
            reason=m.reason,
            npc_id=m.npc_id,
            field=m.field,
            delta=new_int - old_int if isinstance(old_int, int) and isinstance(new_int, int) else None,
            old_value=old_int,
            new_value=new_int,
        )
    )


async def log_rest(db: AsyncSession, m: RestEffect, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="rest",
            source="rest",
            reason=f"{m.rest_type} rest",
            rest_type=m.rest_type,
            delta=m.hp_after - m.hp_before,
            old_value=m.hp_before,
            new_value=m.hp_after,
        )
    )


# ---------------------------------------------------------------------------
# World mutation logging — consequence system (§7)
# ---------------------------------------------------------------------------


async def log_faction_mutation(db: AsyncSession, m: FactionStandingMutation, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="faction_standing_change",
            source=m.source,
            reason=m.reason,
            faction_id=m.faction_id,
            delta=m.delta,
            old_value=m.old_value,
            new_value=m.new_value,
        )
    )


async def log_relationship_mutation(
    db: AsyncSession, m: RelationshipMutation, *, session_id: str | None = None
) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="relationship_change",
            source=m.source,
            reason=m.reason,
            npc_id=m.npc_id,
            delta=m.delta,
            old_value=m.old_value,
            new_value=m.new_value,
        )
    )


async def log_world_flag_mutation(db: AsyncSession, m: WorldFlagMutation, *, session_id: str | None = None) -> None:
    db.add(
        StateChangeLog(
            id=_id(),
            character_id=m.character_id,
            session_id=session_id,
            change_type="world_flag_set",
            source=m.source,
            reason=m.reason,
            flag=m.flag,
        )
    )
