"""Economy pricing engine.

Computes buy and sell prices from item base value, shop markup,
faction standing, and the world sell-back ratio.

Design doc: docs/economy balance.pdf
- Sell-back base ratio: 50% (configurable per world)
- Faction modifiers: Allied +10%, Friendly +5%, Neutral 0%,
  Unfriendly -10%, Hostile → shop refuses
- Legendary items cannot be purchased
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Faction standing → tier mapping (docs/faction system.pdf §Standing Tiers)
# ---------------------------------------------------------------------------

_STANDING_TIERS: list[tuple[int, str]] = [
    (-100, "hostile"),
    (-50, "unfriendly"),
    (0, "neutral"),
    (50, "friendly"),
    (100, "allied"),
]


def faction_tier(standing: int) -> str:
    """Map a numeric faction standing (-100..100) to a named tier."""
    standing = max(-100, min(100, standing))
    if standing >= 50:
        return "allied"
    if standing >= 20:
        return "friendly"
    if standing >= -19:
        return "neutral"
    if standing >= -49:
        return "unfriendly"
    return "hostile"


# Faction tier → sell-back ratio adjustment (additive on the 0.5 base)
_FACTION_SELL_MODIFIER: dict[str, float] = {
    "allied": 0.10,
    "friendly": 0.05,
    "neutral": 0.0,
    "unfriendly": -0.10,
    "hostile": 0.0,  # shop refuses entirely — handled at call site
}

# Faction tier → buy price multiplier (markup adjustment)
_FACTION_BUY_MODIFIER: dict[str, float] = {
    "allied": -0.10,
    "friendly": -0.05,
    "neutral": 0.0,
    "unfriendly": 0.10,
    "hostile": 0.0,  # shop refuses
}

_DEFAULT_SELL_BACK_RATIO = 0.50


def compute_buy_price(
    *,
    base_value: int,
    markup_pct: float,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
) -> int:
    """Compute the final buy price for an item.

    Parameters
    ----------
    base_value : int
        The item's ``value`` field from the item definition.
    markup_pct : float
        The shop's ``markup_percentage`` for this item (e.g. 10.0 for 10%).
    faction_standing : int | None
        Direct standing value. If None, looked up from *character_faction_standing*.
    faction_id : str | None
        Faction ID to look up in the character's faction_standing dict.
    character_faction_standing : dict[str, int] | None
        The character's full faction_standing dict.

    Returns
    -------
    int
        Final price (always ≥ 1).
    """
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    tier = faction_tier(standing)

    markup_mult = 1.0 + (markup_pct / 100.0)
    faction_mult = 1.0 + _FACTION_BUY_MODIFIER[tier]
    price = base_value * markup_mult * faction_mult
    return max(1, round(price))


def compute_sell_price(
    *,
    base_value: int,
    sell_back_ratio: float = _DEFAULT_SELL_BACK_RATIO,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
) -> int:
    """Compute the sell price for an item.

    Returns
    -------
    int
        Final sell price (always ≥ 0 — 0 means the item has no sell value).
    """
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    tier = faction_tier(standing)

    effective_ratio = sell_back_ratio + _FACTION_SELL_MODIFIER[tier]
    effective_ratio = max(0.0, min(1.0, effective_ratio))
    return max(0, math.floor(base_value * effective_ratio))


def is_hostile(
    *,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
) -> bool:
    """Return True if the faction standing is hostile (shop refuses trade)."""
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    return faction_tier(standing) == "hostile"


def _resolve_standing(
    direct: int | None,
    faction_id: str | None,
    standings: dict[str, int] | None,
) -> int:
    """Resolve the standing value from either a direct int or a lookup."""
    if direct is not None:
        return direct
    if faction_id and standings:
        return standings.get(faction_id, 0)
    return 0
