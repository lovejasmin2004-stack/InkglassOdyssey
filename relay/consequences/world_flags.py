"""World flags — per-character state flags for consequence and narrative systems.

Flags are string keys (e.g. "wanted_in:market_district", "bounty_active")
with optional string values.  They track world-state changes caused by player
actions and are read by:

  - The narrative director escalation rules (§7)
  - The prompt builder for NPC awareness (§9)
  - Scenario prerequisite checks

Flags use upsert semantics: setting a flag that already exists updates its
value and reason rather than creating a duplicate.

Design doc: docs/design_proposals.md §7 (Consequence System)
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import WorldFlag

logger = logging.getLogger(__name__)

VALID_SOURCES = frozenset(
    {
        "consequence",
        "quest",
        "scenario",
        "admin",
        "narrative_director",
    }
)


async def get_flag(
    db: AsyncSession,
    character_id: str,
    flag: str,
) -> WorldFlag | None:
    """Load a single flag, or None if not set."""
    result = await db.execute(
        select(WorldFlag).where(
            WorldFlag.character_id == character_id,
            WorldFlag.flag == flag,
        )
    )
    return result.scalar_one_or_none()


async def get_flags(
    db: AsyncSession,
    character_id: str,
    *,
    prefix: str | None = None,
) -> list[WorldFlag]:
    """Load all flags for a character, optionally filtered by prefix.

    Example: ``get_flags(db, char_id, prefix="wanted_in:")`` returns all
    "wanted_in:*" flags.
    """
    query = select(WorldFlag).where(WorldFlag.character_id == character_id)
    if prefix is not None:
        query = query.where(WorldFlag.flag.startswith(prefix))
    result = await db.execute(query)
    return list(result.scalars().all())


async def set_flag(
    db: AsyncSession,
    character_id: str,
    flag: str,
    *,
    value: str = "true",
    reason: str = "",
    source: str = "consequence",
) -> WorldFlag:
    """Set a world flag (upsert). Returns the flag row.

    If the flag already exists, updates its value, reason, and source.
    Does NOT flush/commit — the caller controls the transaction.
    """
    existing = await get_flag(db, character_id, flag)
    if existing is not None:
        existing.value = value
        existing.reason = reason
        existing.source = source
        logger.info(
            "World flag updated",
            extra={
                "character_id": character_id,
                "flag": flag,
                "value": value,
                "reason": reason,
            },
        )
        return existing

    row = WorldFlag(
        id=str(uuid.uuid4()),
        character_id=character_id,
        flag=flag,
        value=value,
        reason=reason,
        source=source,
    )
    db.add(row)
    logger.info(
        "World flag set",
        extra={
            "character_id": character_id,
            "flag": flag,
            "value": value,
            "reason": reason,
        },
    )
    return row


async def clear_flag(
    db: AsyncSession,
    character_id: str,
    flag: str,
) -> bool:
    """Remove a world flag. Returns True if the flag existed."""
    existing = await get_flag(db, character_id, flag)
    if existing is None:
        return False
    await db.delete(existing)
    logger.info(
        "World flag cleared",
        extra={"character_id": character_id, "flag": flag},
    )
    return True


async def has_flag(
    db: AsyncSession,
    character_id: str,
    flag: str,
) -> bool:
    """Check if a flag is set."""
    return await get_flag(db, character_id, flag) is not None


async def count_flags(
    db: AsyncSession,
    character_id: str,
    prefix: str,
) -> int:
    """Count flags matching a prefix.

    Useful for escalation rules: ``count_flags(db, char_id, "npc_killed:")``
    to check how many NPCs have been killed.
    """
    flags = await get_flags(db, character_id, prefix=prefix)
    return len(flags)
