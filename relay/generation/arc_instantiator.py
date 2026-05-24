"""Arc instantiator — creates a concrete event arc instance from a blueprint.

Selects phases from the pool, enforces selection rules, assigns examiners
and terrain, seeds the NPC roster.  All randomization uses a seeded RNG
for deterministic testing.

Mechanical decisions come from rules, not the LLM (Invariant #8).

Design doc: docs/design_proposals.md §4 (Blueprint Pattern for Event Arcs)
"""

from __future__ import annotations

import logging
import random
import uuid

from relay.schemas import (
    ArcCandidate,
    EventArcBlueprint,
    EventArcInstance,
    PhaseInstance,
    PhaseTemplate,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SELECTION_ATTEMPTS = 100  # prevent infinite loops in constrained selection


class InstantiationError(Exception):
    """Arc instantiation failed due to unsatisfiable constraints."""


# ---------------------------------------------------------------------------
# Phase selection
# ---------------------------------------------------------------------------


def _satisfies_must_include(
    selected: list[PhaseTemplate],
    must_include: list[str],
) -> bool:
    """Check that at least one phase from each required category is present."""
    selected_categories = {p.category for p in selected}
    return all(cat in selected_categories for cat in must_include)


def _satisfies_final_phase(
    selected: list[PhaseTemplate],
    final_categories: list[str],
) -> bool:
    """Check that the final phase is from an allowed category."""
    if not final_categories or not selected:
        return True
    return selected[-1].category in final_categories


def _satisfies_max_consecutive(
    selected: list[PhaseTemplate],
    max_consecutive: int,
) -> bool:
    """Check that no more than N consecutive phases share a category."""
    if len(selected) <= 1:
        return True
    run = 1
    for i in range(1, len(selected)):
        if selected[i].category == selected[i - 1].category:
            run += 1
            if run > max_consecutive:
                return False
        else:
            run = 1
    return True


def _satisfies_excluded_combinations(
    selected: list[PhaseTemplate],
    excluded: list[list[str]],
) -> bool:
    """Check that no pair of excluded phase_ids both appear."""
    selected_ids = {p.phase_id for p in selected}
    return all(
        not (len(pair) == 2 and pair[0] in selected_ids and pair[1] in selected_ids)
        for pair in excluded
    )


def _validate_selection(
    selected: list[PhaseTemplate],
    blueprint: EventArcBlueprint,
) -> bool:
    """Validate a phase selection against all rules."""
    rules = blueprint.selection_rules
    return (
        _satisfies_must_include(selected, rules.must_include_categories)
        and _satisfies_final_phase(selected, rules.final_phase_categories)
        and _satisfies_max_consecutive(selected, rules.max_consecutive_same_category)
        and _satisfies_excluded_combinations(selected, rules.excluded_combinations)
    )


def select_phases(
    blueprint: EventArcBlueprint,
    *,
    rng: random.Random | None = None,
) -> list[PhaseTemplate]:
    """Select and order phases from the blueprint's pool.

    Uses rejection sampling: randomly pick phases, check rules, retry.
    For typical blueprints (6-10 phases, simple rules) this converges
    quickly.  Falls back to greedy construction if rejection sampling
    fails.

    Parameters
    ----------
    blueprint
        The event arc blueprint.
    rng
        Optional seeded Random for deterministic testing.

    Returns
    -------
    list[PhaseTemplate]
        Ordered list of selected phases.

    Raises
    ------
    InstantiationError
        If no valid selection can be found.
    """
    rng = rng or random.Random()
    pool = list(blueprint.phase_pool)
    lo, hi = blueprint.phase_count_range
    phase_count = rng.randint(lo, hi)

    if phase_count > len(pool):
        phase_count = len(pool)

    rules = blueprint.selection_rules

    # --- Rejection sampling ---
    for _ in range(_MAX_SELECTION_ATTEMPTS):
        selected = rng.sample(pool, phase_count)
        rng.shuffle(selected)

        # Try to fix final phase constraint without full re-roll
        if rules.final_phase_categories and selected[-1].category not in rules.final_phase_categories:
            # Find a valid final phase in the selection and swap to end
            for i in range(len(selected) - 1):
                if selected[i].category in rules.final_phase_categories:
                    selected[i], selected[-1] = selected[-1], selected[i]
                    break

        if _validate_selection(selected, blueprint):
            return selected

    # --- Greedy fallback ---
    return _greedy_select(blueprint, phase_count, rng)


def _greedy_select(
    blueprint: EventArcBlueprint,
    phase_count: int,
    rng: random.Random,
) -> list[PhaseTemplate]:
    """Greedy phase selection — used when rejection sampling fails.

    Builds the selection one phase at a time, respecting constraints.
    """
    pool = list(blueprint.phase_pool)
    rules = blueprint.selection_rules
    selected: list[PhaseTemplate] = []
    used_ids: set[str] = set()
    excluded_ids: set[str] = set()

    # Build a quick lookup for excluded combos
    excluded_map: dict[str, set[str]] = {}
    for pair in rules.excluded_combinations:
        if len(pair) == 2:
            excluded_map.setdefault(pair[0], set()).add(pair[1])
            excluded_map.setdefault(pair[1], set()).add(pair[0])

    # Reserve a slot for must-include categories
    must_include = list(rules.must_include_categories)

    # First, ensure we have at least one phase from each must-include category
    for cat in must_include:
        candidates = [p for p in pool if p.category == cat and p.phase_id not in used_ids]
        if candidates:
            pick = rng.choice(candidates)
            selected.append(pick)
            used_ids.add(pick.phase_id)
            # Mark exclusions
            for ex_id in excluded_map.get(pick.phase_id, set()):
                excluded_ids.add(ex_id)

    # Fill remaining slots
    while len(selected) < phase_count:
        candidates = [p for p in pool if p.phase_id not in used_ids and p.phase_id not in excluded_ids]
        if not candidates:
            break

        # Filter by max consecutive
        if selected and rules.max_consecutive_same_category:
            run_cat = selected[-1].category
            run_len = 1
            for i in range(len(selected) - 2, -1, -1):
                if selected[i].category == run_cat:
                    run_len += 1
                else:
                    break
            if run_len >= rules.max_consecutive_same_category:
                candidates = [p for p in candidates if p.category != run_cat]

        if not candidates:
            break

        pick = rng.choice(candidates)
        selected.append(pick)
        used_ids.add(pick.phase_id)
        for ex_id in excluded_map.get(pick.phase_id, set()):
            excluded_ids.add(ex_id)

    # Fix final phase constraint
    if rules.final_phase_categories and selected and selected[-1].category not in rules.final_phase_categories:
        for i in range(len(selected) - 1):
            if selected[i].category in rules.final_phase_categories:
                selected[i], selected[-1] = selected[-1], selected[i]
                break

    if not selected:
        raise InstantiationError(f"Cannot select any valid phases for blueprint '{blueprint.id}'")

    return selected


# ---------------------------------------------------------------------------
# NPC roster seeding
# ---------------------------------------------------------------------------


def seed_roster(
    blueprint: EventArcBlueprint,
    *,
    rng: random.Random | None = None,
) -> list[ArcCandidate]:
    """Create the initial NPC candidate roster.

    Anchor NPCs always appear with is_anchor=True.
    Background NPC count is randomized within generated_count_range.
    Background NPC IDs are generated as placeholders — the caller can
    resolve them to actual generated NPCs via the template generation
    system (§1).

    Parameters
    ----------
    blueprint
        The event arc blueprint with roster configuration.
    rng
        Optional seeded Random.

    Returns
    -------
    list[ArcCandidate]
        The initial candidate roster.
    """
    rng = rng or random.Random()
    roster = blueprint.npc_roster
    candidates: list[ArcCandidate] = []

    # Anchor NPCs always present
    for npc_id in roster.anchor_npcs:
        candidates.append(
            ArcCandidate(
                npc_id=npc_id,
                is_anchor=True,
                status="active",
                relationship_score=0,
            )
        )

    # Generated background NPCs
    if roster.generated_count_range:
        lo, hi = roster.generated_count_range
        gen_count = rng.randint(lo, hi)
        roles = roster.generated_template_roles

        for _i in range(gen_count):
            # Generate placeholder IDs — to be resolved by template generation
            role = rng.choice(roles) if roles else "generic"
            gen_id = f"gen_{role}_{uuid.uuid4().hex[:8]}"
            candidates.append(
                ArcCandidate(
                    npc_id=gen_id,
                    is_anchor=False,
                    status="active",
                    relationship_score=0,
                )
            )

    return candidates


# ---------------------------------------------------------------------------
# Full instantiation
# ---------------------------------------------------------------------------


def instantiate_arc(
    blueprint: EventArcBlueprint,
    character_id: str,
    *,
    rng: random.Random | None = None,
    instance_id: str | None = None,
    session_id: str | None = None,
) -> EventArcInstance:
    """Create a concrete event arc instance from a blueprint.

    Selects phases, assigns examiners/terrain/hazards per phase,
    seeds the NPC roster, and returns a fully valid EventArcInstance.

    Parameters
    ----------
    blueprint
        The event arc blueprint.
    character_id
        The player character this arc belongs to.
    rng
        Optional seeded Random for deterministic testing.
    instance_id
        Optional explicit ID.  If not provided, generates a UUID-based one.
    session_id
        Optional session ID to associate with this arc.

    Returns
    -------
    EventArcInstance
        A fully schema-valid arc instance, ready to persist.

    Raises
    ------
    InstantiationError
        If phase selection fails due to unsatisfiable constraints.
    """
    rng = rng or random.Random()

    # --- Identity ---
    instance_id = instance_id or f"{blueprint.id}_{character_id}_{uuid.uuid4().hex[:8]}"

    # --- Select phases ---
    selected_phases = select_phases(blueprint, rng=rng)

    # --- Assign per-phase details ---
    phase_instances: list[PhaseInstance] = []
    for i, phase in enumerate(selected_phases):
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

    # --- Seed roster ---
    candidates = seed_roster(blueprint, rng=rng)

    # --- Assemble instance ---
    instance = EventArcInstance(
        id=instance_id,
        blueprint_id=blueprint.id,
        world_id=blueprint.world_id,
        character_id=character_id,
        origin="authored",
        status="active",
        current_phase_index=0,
        phases=phase_instances,
        candidates=candidates,
        session_id=session_id,
    )

    logger.info(
        "Event arc instantiated",
        extra={
            "instance_id": instance_id,
            "blueprint_id": blueprint.id,
            "character_id": character_id,
            "phase_count": len(phase_instances),
            "candidate_count": len(candidates),
        },
    )

    return instance
