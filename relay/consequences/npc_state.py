"""NPC instance state — per-player tracking of NPC status and disposition.

Separate from NPC personality files (which define who the NPC *is*), instance
state tracks what *happened* to this NPC in this player's world: attacked,
injured, fled, killed, befriended, etc.

Created lazily — no row exists until the first meaningful consequence event.
The absence of a row means the NPC is in their default state (alive, neutral
disposition, no flags).

Design doc: docs/design_proposals.md §7 (Consequence System)
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import NpcInstanceState

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({"alive", "injured", "fled", "dead", "defeated"})


async def get_instance(
    db: AsyncSession,
    character_id: str,
    npc_id: str,
) -> NpcInstanceState | None:
    """Load the instance state for an NPC, or None if untracked."""
    result = await db.execute(
        select(NpcInstanceState).where(
            NpcInstanceState.character_id == character_id,
            NpcInstanceState.npc_id == npc_id,
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_instance(
    db: AsyncSession,
    character_id: str,
    npc_id: str,
    *,
    world_id: str,
) -> NpcInstanceState:
    """Load an existing instance or create a default one.

    Does NOT flush/commit — the caller controls the transaction.
    """
    instance = await get_instance(db, character_id, npc_id)
    if instance is not None:
        return instance

    instance = NpcInstanceState(
        id=str(uuid.uuid4()),
        character_id=character_id,
        npc_id=npc_id,
        world_id=world_id,
        status="alive",
        flags=[],
    )
    db.add(instance)
    logger.info(
        "NPC instance state created",
        extra={"character_id": character_id, "npc_id": npc_id},
    )
    return instance


async def get_instances_for_character(
    db: AsyncSession,
    character_id: str,
) -> list[NpcInstanceState]:
    """Load all NPC instance states for a character."""
    result = await db.execute(
        select(NpcInstanceState).where(
            NpcInstanceState.character_id == character_id,
        )
    )
    return list(result.scalars().all())


def update_status(
    instance: NpcInstanceState,
    new_status: str,
    *,
    reason: str = "",
) -> str:
    """Update the NPC's status. Returns the old status.

    Raises ValueError if new_status is not in VALID_STATUSES.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid NPC status: {new_status!r}. Must be one of {VALID_STATUSES}")
    old = instance.status
    instance.status = new_status
    if reason:
        instance.last_interaction_summary = reason
    logger.info(
        "NPC status updated",
        extra={
            "npc_id": instance.npc_id,
            "old_status": old,
            "new_status": new_status,
            "reason": reason,
        },
    )
    return old


def add_flag(instance: NpcInstanceState, flag: str) -> bool:
    """Add a flag to the NPC instance. Returns True if the flag was new."""
    current = list(instance.flags or [])
    if flag in current:
        return False
    current.append(flag)
    instance.flags = current
    logger.debug("NPC flag added", extra={"npc_id": instance.npc_id, "flag": flag})
    return True


def remove_flag(instance: NpcInstanceState, flag: str) -> bool:
    """Remove a flag from the NPC instance. Returns True if the flag existed."""
    current = list(instance.flags or [])
    if flag not in current:
        return False
    current.remove(flag)
    instance.flags = current
    logger.debug("NPC flag removed", extra={"npc_id": instance.npc_id, "flag": flag})
    return True


def has_flag(instance: NpcInstanceState, flag: str) -> bool:
    """Check whether the NPC instance has a specific flag."""
    return flag in (instance.flags or [])


def set_disposition_override(
    instance: NpcInstanceState,
    value: int,
) -> int | None:
    """Set a disposition override. Returns the old value (None if unset)."""
    old = instance.disposition_override
    # Clamp to [-100, 100] — same range as relationship scores
    instance.disposition_override = max(-100, min(100, value))
    return old
