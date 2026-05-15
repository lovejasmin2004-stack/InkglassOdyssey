"""Combat lifecycle hooks.

Pattern adopted from Foundry dnd5e's Combat5e (_onStartTurn, _onEndTurn,
_onStartRound). These are the canonical seams for time-bounded effects so
duration ticks live in one place instead of being scattered across endpoints.

Each hook takes the current conditions list and returns the next list. They
are pure -- callers persist the result. Endpoint integration (death-save
evaluation, recovery hooks, concentration checks) will layer on top in later
steps; for Phase 0 the hooks own duration tracking.
"""

from __future__ import annotations

import logging

from relay.combat.conditions import ConditionInstance, tick_durations

logger = logging.getLogger(__name__)


def on_turn_start(conditions: list[ConditionInstance]) -> list[ConditionInstance]:
    """Called at the start of a combatant's turn.

    Currently: no-op for durations (turn-scoped effects expire at turn end).
    Reserved for future per-turn-start triggers (e.g., damage-over-time ticks).
    """
    return list(conditions)


def on_turn_end(conditions: list[ConditionInstance]) -> list[ConditionInstance]:
    """Called at the end of a combatant's turn.

    Decrements duration on turn-unit conditions; expired conditions and their
    riders are removed.
    """
    return tick_durations(conditions, unit="turns")


def on_round_start(conditions: list[ConditionInstance]) -> list[ConditionInstance]:
    """Called at the start of a combat round.

    Decrements duration on round-unit conditions.
    """
    return tick_durations(conditions, unit="rounds")
