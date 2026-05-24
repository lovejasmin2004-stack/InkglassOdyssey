"""Custom arc assembler — player-assembled arcs from unlocked building blocks.

Players pick a blueprint, choose specific phases or "surprise me", select
a location, and optionally feature NPCs they've met.  The assembler validates
against blueprint selection rules, fills surprise-me slots, and produces
a standard EventArcInstance with origin="custom".

Custom arcs give XP and loot but don't unlock tier gates (origin flag).

Design doc: docs/design_proposals.md §5 (Custom Arc Remixing)
"""

from __future__ import annotations

import logging
import random
import uuid

from relay.generation.arc_instantiator import (
    _validate_selection,
    seed_roster,
)
from relay.schemas import (
    ArcCandidate,
    EventArcBlueprint,
    EventArcInstance,
    PhaseInstance,
    PhaseTemplate,
)

logger = logging.getLogger(__name__)

# Sentinel value for "surprise me" slots
SURPRISE_ME = "surprise_me"


class CustomArcError(Exception):
    """Custom arc assembly failed validation."""


def _resolve_phase(phase_id: str, blueprint: EventArcBlueprint) -> PhaseTemplate | None:
    """Look up a phase template by ID in the blueprint's pool."""
    for phase in blueprint.phase_pool:
        if phase.phase_id == phase_id:
            return phase
    return None


def validate_custom_arc_request(
    blueprint: EventArcBlueprint,
    phase_choices: list[str],
    region_id: str | None = None,
    featured_npc_ids: list[str] | None = None,
) -> list[str]:
    """Validate a custom arc request and return a list of issues.

    Parameters
    ----------
    blueprint
        The base blueprint being remixed.
    phase_choices
        List of phase_id strings or SURPRISE_ME sentinel.
        Length must be within the blueprint's phase_count_range.
    region_id
        Optional override region.  If not given, uses blueprint default.
    featured_npc_ids
        NPC IDs to feature as anchor candidates.

    Returns
    -------
    list[str]
        Empty list if valid, otherwise list of human-readable error messages.
    """
    errors: list[str] = []

    # Phase count
    lo, hi = blueprint.phase_count_range
    if len(phase_choices) < lo or len(phase_choices) > hi:
        errors.append(f"Phase count {len(phase_choices)} out of range [{lo}, {hi}].")

    # Validate chosen phase IDs exist in the pool
    pool_ids = {p.phase_id for p in blueprint.phase_pool}
    chosen_ids = [pid for pid in phase_choices if pid != SURPRISE_ME]
    for pid in chosen_ids:
        if pid not in pool_ids:
            errors.append(f"Phase '{pid}' not in blueprint's phase pool.")

    # Check for duplicate explicit choices
    if len(chosen_ids) != len(set(chosen_ids)):
        errors.append("Duplicate phase selections are not allowed.")

    return errors


def assemble_custom_arc(
    blueprint: EventArcBlueprint,
    character_id: str,
    phase_choices: list[str],
    *,
    region_id: str | None = None,
    featured_npc_ids: list[str] | None = None,
    rng: random.Random | None = None,
    instance_id: str | None = None,
    session_id: str | None = None,
) -> EventArcInstance:
    """Assemble a custom arc instance from player choices.

    The player specifies:
    - A blueprint (base template)
    - Phase slots: specific phase_ids or SURPRISE_ME
    - Optional region override
    - Optional featured NPCs to include as anchors

    The assembler:
    1. Validates the request
    2. Resolves explicit phase choices
    3. Fills SURPRISE_ME slots with random picks from remaining pool
    4. Validates the final selection against blueprint rules
    5. Assigns examiners/terrain/hazards per phase
    6. Seeds the roster (blueprint anchors + featured NPCs + generated)
    7. Returns a standard EventArcInstance with origin="custom"

    Parameters
    ----------
    blueprint
        The base blueprint being remixed.
    character_id
        The player character assembling this arc.
    phase_choices
        List of phase_id strings or SURPRISE_ME.
    region_id
        Optional region override (defaults to blueprint's region).
    featured_npc_ids
        NPCs the player wants featured (added as non-anchor candidates).
    rng
        Optional seeded Random for deterministic testing.
    instance_id
        Optional explicit instance ID.
    session_id
        Optional session ID.

    Returns
    -------
    EventArcInstance
        A custom arc instance with origin="custom".

    Raises
    ------
    CustomArcError
        If the request is invalid or the selection violates blueprint rules.
    """
    rng = rng or random.Random()

    # --- Step 1: Validate ---
    issues = validate_custom_arc_request(blueprint, phase_choices, region_id, featured_npc_ids)
    if issues:
        raise CustomArcError(f"Custom arc validation failed: {'; '.join(issues)}")

    # --- Step 2: Resolve explicit choices ---
    resolved: list[PhaseTemplate | None] = []
    used_ids: set[str] = set()

    for pid in phase_choices:
        if pid == SURPRISE_ME:
            resolved.append(None)  # placeholder for random fill
        else:
            phase = _resolve_phase(pid, blueprint)
            if phase is None:
                raise CustomArcError(f"Phase '{pid}' not found in blueprint pool.")
            resolved.append(phase)
            used_ids.add(pid)

    # --- Step 3: Fill SURPRISE_ME slots ---
    available = [p for p in blueprint.phase_pool if p.phase_id not in used_ids]

    for i, phase in enumerate(resolved):
        if phase is None:
            if not available:
                raise CustomArcError("Not enough phases in the pool to fill all 'surprise me' slots.")
            pick = rng.choice(available)
            resolved[i] = pick
            available.remove(pick)
            used_ids.add(pick.phase_id)

    # At this point all slots should be filled
    selected: list[PhaseTemplate] = [p for p in resolved if p is not None]

    # --- Step 4: Validate against blueprint rules ---
    if not _validate_selection(selected, blueprint):
        # First, try reordering the phases (player chose WHICH phases, order is flexible)
        reordered = _try_reorder(selected, blueprint, rng)
        if reordered is not None:
            selected = reordered
        else:
            # Then try re-rolling surprise-me slots
            surprise_indices = [i for i, pid in enumerate(phase_choices) if pid == SURPRISE_ME]
            fixed = _try_fix_selection(selected, blueprint, surprise_indices, used_ids, rng)
            if fixed is None:
                raise CustomArcError(
                    "Selected phases violate blueprint rules and could not be fixed. "
                    "Try using more 'surprise me' slots or different phase choices."
                )
            selected = fixed

    # --- Step 5: Build phase instances ---
    instance_id = instance_id or f"{blueprint.id}_custom_{character_id}_{uuid.uuid4().hex[:8]}"

    phase_instances: list[PhaseInstance] = []
    for i, phase in enumerate(selected):
        examiner = rng.choice(phase.examiner_pool) if phase.examiner_pool else None
        terrain = rng.choice(phase.terrain_pool) if phase.terrain_pool else None
        hazards: list[str] = []
        if phase.hazard_pool:
            hazard_count = min(rng.randint(0, 2), len(phase.hazard_pool))
            hazards = rng.sample(phase.hazard_pool, hazard_count)

        phase_instances.append(
            PhaseInstance(
                phase_id=phase.phase_id,
                status="active" if i == 0 else "pending",
                examiner_npc_id=examiner,
                terrain=terrain,
                hazards=hazards,
            )
        )

    # --- Step 6: Seed roster ---
    candidates = seed_roster(blueprint, rng=rng)

    # Add featured NPCs as non-anchor candidates
    if featured_npc_ids:
        existing_ids = {c.npc_id for c in candidates}
        for npc_id in featured_npc_ids:
            if npc_id not in existing_ids:
                candidates.append(
                    ArcCandidate(
                        npc_id=npc_id,
                        is_anchor=False,
                        status="active",
                        relationship_score=0,
                    )
                )

    # --- Step 7: Assemble instance ---
    instance = EventArcInstance(
        id=instance_id,
        blueprint_id=blueprint.id,
        world_id=blueprint.world_id,
        character_id=character_id,
        origin="custom",
        status="active",
        current_phase_index=0,
        phases=phase_instances,
        candidates=candidates,
        session_id=session_id,
    )

    logger.info(
        "Custom arc assembled",
        extra={
            "instance_id": instance_id,
            "blueprint_id": blueprint.id,
            "character_id": character_id,
            "phase_count": len(phase_instances),
            "surprise_me_count": sum(1 for p in phase_choices if p == SURPRISE_ME),
            "featured_npcs": len(featured_npc_ids) if featured_npc_ids else 0,
        },
    )

    return instance


def _try_fix_selection(
    selected: list[PhaseTemplate],
    blueprint: EventArcBlueprint,
    surprise_indices: list[int],
    used_ids: set[str],
    rng: random.Random,
    max_attempts: int = 50,
) -> list[PhaseTemplate] | None:
    """Try to fix a failing selection by re-rolling surprise-me slots.

    Only re-rolls slots that were originally SURPRISE_ME — player-chosen
    phases are never changed.

    Returns None if no valid selection found within max_attempts.
    """
    if not surprise_indices:
        return None

    available_pool = [p for p in blueprint.phase_pool if p.phase_id not in used_ids]
    # Also include the current surprise-me picks as available for re-roll
    for idx in surprise_indices:
        if selected[idx].phase_id not in {p.phase_id for p in available_pool}:
            available_pool.append(selected[idx])

    for _ in range(max_attempts):
        candidate = list(selected)
        local_available = list(available_pool)
        rng.shuffle(local_available)

        valid = True
        for idx in surprise_indices:
            if not local_available:
                valid = False
                break
            pick = local_available.pop()
            candidate[idx] = pick

        if valid and _validate_selection(candidate, blueprint):
            return candidate

    return None


def _try_reorder(
    selected: list[PhaseTemplate],
    blueprint: EventArcBlueprint,
    rng: random.Random,
    max_attempts: int = 50,
) -> list[PhaseTemplate] | None:
    """Try to reorder phases to satisfy blueprint rules.

    The player chose WHICH phases they want — the system can reorder
    them to satisfy constraints like final_phase_categories and
    max_consecutive_same_category.

    Returns None if no valid ordering found.
    """
    for _ in range(max_attempts):
        candidate = list(selected)
        rng.shuffle(candidate)

        # Try to fix final phase constraint specifically
        rules = blueprint.selection_rules
        if rules.final_phase_categories and candidate[-1].category not in rules.final_phase_categories:
            for i in range(len(candidate) - 1):
                if candidate[i].category in rules.final_phase_categories:
                    candidate[i], candidate[-1] = candidate[-1], candidate[i]
                    break

        if _validate_selection(candidate, blueprint):
            return candidate

    return None
