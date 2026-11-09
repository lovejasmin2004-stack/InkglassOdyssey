"""Companion ambient behaviour — unprompted comments and mood.

Companions comment based on trigger_categories and comment_frequency.
Mood modifier adjusts tone based on relationship score and loyalty_strain.

Design doc: docs/companion system.pdf §Ambient Behaviour
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

VALID_TRIGGERS = frozenset(
    {
        "new_region",
        "npc_interaction_start",
        "combat_start",
        "player_idle",
        "weather_change",
        "world_event",
    }
)

DEFAULT_COMMENT_PROBABILITY = 0.3


def should_trigger_comment(
    *,
    trigger: str,
    companion_data: dict,
    turn_number: int = 0,
) -> bool:
    """Determine whether the companion should comment for this trigger.

    Uses ambient_behavior.comment_frequency:
      - "every_N" → triggers every N turns
      - "probability_X" → random chance X (0.0-1.0)
      - bare float string → treated as probability
    """
    if trigger not in VALID_TRIGGERS:
        logger.warning("Unknown trigger %r, valid: %s", trigger, VALID_TRIGGERS)
        return False

    ambient = companion_data.get("ambient_behavior", {})
    categories = ambient.get("trigger_categories", [])

    if trigger not in categories:
        return False

    frequency = ambient.get("comment_frequency", "")
    return _evaluate_frequency(frequency, turn_number)


def _evaluate_frequency(frequency: str, turn_number: int) -> bool:
    if not frequency:
        return random.random() < DEFAULT_COMMENT_PROBABILITY

    if frequency.startswith("every_"):
        try:
            n = int(frequency.removeprefix("every_"))
            if n <= 0:
                logger.warning("Invalid frequency %r: period must be positive", frequency)
                return False
            return turn_number % n == 0
        except ValueError:
            return False

    if frequency.startswith("probability_"):
        try:
            p = float(frequency.removeprefix("probability_"))
            return random.random() < p
        except ValueError:
            return False

    try:
        p = float(frequency)
        return random.random() < p
    except ValueError:
        return random.random() < DEFAULT_COMMENT_PROBABILITY


def compute_mood_modifier(
    *,
    companion_data: dict,
    relationship_score: int,
    loyalty_strain: int,
) -> float:
    """Compute the mood modifier for tone adjustment.

    Base modifier from companion_data.ambient_behavior.mood_modifier,
    adjusted down by loyalty_strain and up/down by relationship_score.

    Returns a float in [-1.0, 1.0] range.
    """
    ambient = companion_data.get("ambient_behavior", {})
    base = ambient.get("mood_modifier", 0.0) or 0.0

    relationship_factor = relationship_score / 100.0
    strain_penalty = loyalty_strain * -0.1

    mood = base + relationship_factor + strain_penalty
    return max(-1.0, min(1.0, mood))


def find_world_event_reaction(
    companion_data: dict,
    event_id: str,
) -> dict | None:
    """Find a matching world event reaction, if any."""
    reactions = companion_data.get("world_event_reactions") or []
    for reaction in reactions:
        if reaction.get("event_id") == event_id:
            return reaction
    return None
