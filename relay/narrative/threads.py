"""Narrative thread tracking — Layer 2 of the Three-Layer Narrative Model.

The LLM proposes narrative_signals during scene analysis (Call 1).  The relay
validates them (Invariant #8: LLM is never authoritative), persists them as
NarrativeThread rows, and feeds active threads back into future prompts via
GameStateContext.

Thread lifecycle:
  - LLM emits a signal → relay validates → upsert_thread (create or bump)
  - Narrative director reads active threads → writes director_signal hints
  - Player/DM can resolve threads; long-idle threads go dormant

Design doc: docs/design_proposals.md §6 (Three-Layer Narrative Model)
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import NarrativeThread

logger = logging.getLogger(__name__)

VALID_SIGNAL_TYPES = frozenset({"commitment", "interest", "revelation", "tension"})
VALID_THREAD_STATUSES = frozenset({"active", "resolved", "dormant"})

# Guard rails — don't trust raw LLM output
_MAX_SIGNALS_PER_TURN = 3
_MAX_THREAD_KEY_LENGTH = 80
_MAX_SUMMARY_LENGTH = 300
_MAX_RELATED_IDS = 5

# Regex: only lowercase ASCII + digits + underscores, 2–80 chars
_THREAD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")


# ---------------------------------------------------------------------------
# Validation (same philosophy as validate_checks_batch / validate_world_mutations)
# ---------------------------------------------------------------------------


def validate_narrative_signals(raw_signals: list[dict]) -> list[dict]:
    """Validate and sanitise raw narrative_signals from LLM output.

    Returns a list of validated signal dicts, dropping any that are
    malformed or exceed limits.  Never raises — bad signals are logged
    and skipped.
    """
    if not isinstance(raw_signals, list):
        logger.warning("narrative_signals is not a list, ignoring")
        return []

    validated: list[dict] = []
    for raw in raw_signals:
        if len(validated) >= _MAX_SIGNALS_PER_TURN:
            logger.debug(
                "Narrative signals capped at %d per turn",
                _MAX_SIGNALS_PER_TURN,
            )
            break

        if not isinstance(raw, dict):
            continue

        signal_type = raw.get("type", "")
        if signal_type not in VALID_SIGNAL_TYPES:
            logger.debug("Invalid narrative signal type: %s", signal_type)
            continue

        summary = raw.get("summary", "")
        if not isinstance(summary, str) or not summary.strip():
            logger.debug("Narrative signal missing summary")
            continue
        summary = summary.strip()[:_MAX_SUMMARY_LENGTH]

        thread_key = raw.get("thread_key", "")
        if not isinstance(thread_key, str):
            continue
        thread_key = thread_key.strip().lower()[:_MAX_THREAD_KEY_LENGTH]
        if not _THREAD_KEY_RE.match(thread_key):
            logger.debug("Invalid thread_key format: %s", thread_key)
            continue

        # Related NPCs / regions — optional, cap and sanitise
        related_npcs = _sanitise_id_list(raw.get("related_npcs", []))
        related_regions = _sanitise_id_list(raw.get("related_regions", []))

        validated.append(
            {
                "type": signal_type,
                "summary": summary,
                "thread_key": thread_key,
                "related_npcs": related_npcs,
                "related_regions": related_regions,
            }
        )

    return validated


def _sanitise_id_list(raw: object) -> list[str]:
    """Extract a list of string IDs from untrusted LLM output."""
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:80])
        if len(result) >= _MAX_RELATED_IDS:
            break
    return result


# ---------------------------------------------------------------------------
# Persistence (upsert pattern — create or bump mention_count)
# ---------------------------------------------------------------------------


async def upsert_thread(
    db: AsyncSession,
    *,
    character_id: str,
    world_id: str,
    signal: dict,
    session_id: str | None = None,
) -> NarrativeThread:
    """Create a new narrative thread or update an existing one.

    If a thread with the same (character_id, thread_key) already exists:
      - Increment mention_count
      - Update summary to latest
      - Merge related_npcs / related_regions (deduplicate)
      - Set last_seen_session_id
      - Reactivate if dormant

    Returns the created or updated NarrativeThread.
    """
    thread_key = signal["thread_key"]

    result = await db.execute(
        select(NarrativeThread).where(
            NarrativeThread.character_id == character_id,
            NarrativeThread.thread_key == thread_key,
        )
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        existing.mention_count += 1
        existing.summary = signal["summary"]
        existing.signal_type = signal["type"]
        existing.last_seen_session_id = session_id

        # Merge related IDs (deduplicate, cap)
        merged_npcs = list(dict.fromkeys([*existing.related_npcs, *signal.get("related_npcs", [])]))
        existing.related_npcs = merged_npcs[:_MAX_RELATED_IDS]

        merged_regions = list(dict.fromkeys([*existing.related_regions, *signal.get("related_regions", [])]))
        existing.related_regions = merged_regions[:_MAX_RELATED_IDS]

        # Reactivate dormant threads if the player re-engages
        if existing.status == "dormant":
            existing.status = "active"
            logger.info(
                "Narrative thread reactivated",
                extra={"thread_key": thread_key, "character_id": character_id},
            )

        return existing

    # Create new thread
    thread = NarrativeThread(
        id=str(uuid.uuid4()),
        character_id=character_id,
        world_id=world_id,
        thread_key=thread_key,
        signal_type=signal["type"],
        summary=signal["summary"],
        related_npcs=signal.get("related_npcs", []),
        related_regions=signal.get("related_regions", []),
        mention_count=1,
        status="active",
        first_seen_session_id=session_id,
        last_seen_session_id=session_id,
    )
    db.add(thread)

    logger.info(
        "New narrative thread created",
        extra={
            "thread_key": thread_key,
            "signal_type": signal["type"],
            "character_id": character_id,
        },
    )

    return thread


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def get_active_threads(
    db: AsyncSession,
    character_id: str,
    *,
    limit: int = 10,
) -> list[NarrativeThread]:
    """Return active narrative threads for a character, most-mentioned first.

    Capped at *limit* to avoid prompt bloat.  The narrative director can
    use these to write director_signal hints.
    """
    result = await db.execute(
        select(NarrativeThread)
        .where(
            NarrativeThread.character_id == character_id,
            NarrativeThread.status == "active",
        )
        .order_by(NarrativeThread.mention_count.desc(), NarrativeThread.updated_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def resolve_thread(
    db: AsyncSession,
    character_id: str,
    thread_key: str,
) -> bool:
    """Mark a thread as resolved.  Returns True if found and updated."""
    result = await db.execute(
        select(NarrativeThread).where(
            NarrativeThread.character_id == character_id,
            NarrativeThread.thread_key == thread_key,
        )
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        return False
    thread.status = "resolved"
    return True
