"""Consequence evaluator — validate and apply LLM-proposed world mutations.

Takes the ``world_mutations`` array from the scene_analysis tool response,
validates each mutation type, clamps magnitudes, and applies through the
existing faction/relationship/world-flag systems.  All changes are logged
to StateChangeLog via the typed mutation models.

The LLM proposes; the relay validates.  Invariant #8: LLM is never
authoritative over mechanical state.

Design doc: docs/design_proposals.md §7 (Consequence System)
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from relay.companions.relationship import apply_relationship_change
from relay.consequences.world_flags import set_flag
from relay.factions.reputation import apply_standing_change
from relay.mutations import (
    FactionStandingMutation,
    RelationshipMutation,
    WorldFlagMutation,
)
from relay.state_log import (
    log_faction_mutation,
    log_relationship_mutation,
    log_world_flag_mutation,
)

logger = logging.getLogger(__name__)

# Maximum absolute delta the LLM can propose per mutation.
# Larger changes require quest rewards or admin actions.
_MAX_MUTATION_DELTA = 50

VALID_MUTATION_TYPES = frozenset(
    {
        "faction_standing_change",
        "relationship_change",
        "world_flag_set",
    }
)

# Maximum mutations per turn to prevent abuse / LLM runaway.
_MAX_MUTATIONS_PER_TURN = 5


def validate_world_mutations(raw_mutations: list[dict]) -> list[dict]:
    """Validate and clamp LLM-proposed world mutations.

    Drops invalid entries and clamps deltas to [-MAX, +MAX].
    Returns only well-formed mutations.
    """
    validated: list[dict] = []
    for i, m in enumerate(raw_mutations):
        if i >= _MAX_MUTATIONS_PER_TURN:
            logger.warning(
                "World mutations capped at %d per turn, dropping remainder",
                _MAX_MUTATIONS_PER_TURN,
                extra={"total_proposed": len(raw_mutations)},
            )
            break

        mut_type = m.get("type")
        if mut_type not in VALID_MUTATION_TYPES:
            logger.warning(
                "Invalid world mutation type, skipping",
                extra={"type": mut_type, "index": i},
            )
            continue

        reason = m.get("reason", "")
        if not reason:
            logger.warning(
                "World mutation missing reason, skipping",
                extra={"type": mut_type, "index": i},
            )
            continue

        if mut_type == "faction_standing_change":
            faction_id = m.get("faction_id")
            if not faction_id:
                logger.warning("faction_standing_change missing faction_id, skipping")
                continue
            delta = _clamp_delta(m.get("delta", 0))
            if delta == 0:
                continue
            validated.append({"type": mut_type, "faction_id": faction_id, "delta": delta, "reason": reason})

        elif mut_type == "relationship_change":
            npc_id = m.get("npc_id")
            if not npc_id:
                logger.warning("relationship_change missing npc_id, skipping")
                continue
            delta = _clamp_delta(m.get("delta", 0))
            if delta == 0:
                continue
            validated.append({"type": mut_type, "npc_id": npc_id, "delta": delta, "reason": reason})

        elif mut_type == "world_flag_set":
            flag = m.get("flag")
            if not flag:
                logger.warning("world_flag_set missing flag, skipping")
                continue
            validated.append({"type": mut_type, "flag": flag, "reason": reason})

    return validated


def _clamp_delta(raw: int | None) -> int:
    """Clamp a mutation delta to the allowed range."""
    if raw is None:
        return 0
    return max(-_MAX_MUTATION_DELTA, min(_MAX_MUTATION_DELTA, int(raw)))


async def apply_world_mutations(
    db: AsyncSession,
    character_id: str,
    mutations: list[dict],
    *,
    faction_standing: dict[str, int] | None = None,
    relationships: dict[str, int] | None = None,
    faction_registry: dict[str, dict] | None = None,
    session_id: str | None = None,
) -> dict:
    """Apply validated world mutations and log them.

    Parameters
    ----------
    db : AsyncSession
        Database session (caller controls commit).
    character_id : str
        The character these mutations apply to.
    mutations : list[dict]
        Output of ``validate_world_mutations``.
    faction_standing : dict[str, int] | None
        Character's current faction standing dict. Modified in place.
        If None, faction mutations are skipped.
    relationships : dict[str, int] | None
        Character's current NPC relationship scores. Modified in place.
        If None, relationship mutations are skipped.
    faction_registry : dict[str, dict] | None
        Faction definitions for propagation. If None, no propagation.
    session_id : str | None
        Session ID for audit logging.

    Returns
    -------
    dict
        Summary of applied changes with keys:
        ``faction_changes``, ``relationship_changes``, ``flags_set``.
    """
    result: dict = {
        "faction_changes": [],
        "relationship_changes": [],
        "flags_set": [],
    }

    for m in mutations:
        mut_type = m["type"]

        if mut_type == "faction_standing_change" and faction_standing is not None:
            faction_id = m["faction_id"]
            delta = m["delta"]
            old_value = faction_standing.get(faction_id, 0)

            change_report = apply_standing_change(
                faction_standing,
                faction_id,
                delta,
                faction_registry,
                reason=m["reason"],
            )

            new_value = faction_standing.get(faction_id, 0)

            mutation = FactionStandingMutation(
                character_id=character_id,
                faction_id=faction_id,
                delta=delta,
                old_value=old_value,
                new_value=new_value,
                reason=m["reason"],
            )
            await log_faction_mutation(db, mutation, session_id=session_id)

            result["faction_changes"].append(
                {
                    "faction_id": faction_id,
                    "delta": delta,
                    "old": old_value,
                    "new": new_value,
                    "propagation": change_report,
                }
            )

            logger.info(
                "Faction mutation applied",
                extra={
                    "character_id": character_id,
                    "faction_id": faction_id,
                    "delta": delta,
                    "old": old_value,
                    "new": new_value,
                },
            )

        elif mut_type == "relationship_change" and relationships is not None:
            npc_id = m["npc_id"]
            delta = m["delta"]
            old_value = relationships.get(npc_id, 0)

            change_summary = apply_relationship_change(relationships, npc_id, delta)
            new_value = relationships.get(npc_id, 0)

            mutation = RelationshipMutation(
                character_id=character_id,
                npc_id=npc_id,
                delta=delta,
                old_value=old_value,
                new_value=new_value,
                reason=m["reason"],
            )
            await log_relationship_mutation(db, mutation, session_id=session_id)

            result["relationship_changes"].append(change_summary)

            logger.info(
                "Relationship mutation applied",
                extra={
                    "character_id": character_id,
                    "npc_id": npc_id,
                    "delta": delta,
                    "old": old_value,
                    "new": new_value,
                },
            )

        elif mut_type == "world_flag_set":
            flag_name = m["flag"]
            await set_flag(
                db,
                character_id,
                flag_name,
                reason=m["reason"],
                source="consequence",
            )

            mutation = WorldFlagMutation(
                character_id=character_id,
                flag=flag_name,
                reason=m["reason"],
            )
            await log_world_flag_mutation(db, mutation, session_id=session_id)

            result["flags_set"].append({"flag": flag_name, "reason": m["reason"]})

            logger.info(
                "World flag mutation applied",
                extra={
                    "character_id": character_id,
                    "flag": flag_name,
                    "reason": m["reason"],
                },
            )

    return result
