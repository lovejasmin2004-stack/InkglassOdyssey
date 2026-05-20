"""Economy pricing engine.

Computes buy and sell prices from item base value, shop markup,
faction standing, and the world sell-back ratio.

Design doc: docs/economy balance.pdf
- Sell-back base ratio: 50% (configurable per world)
- Faction modifiers: Allied -20%, Friendly -10%, Neutral 0%,
  Unfriendly +25%, Hostile → shop refuses
- Legendary items cannot be purchased

Rounding convention:
  - Buy price: ``round()`` — standard banker's rounding.
  - Sell price: ``math.floor()`` — always rounds down.
  The asymmetry is intentional: selling at floor ensures the economy is
  a consistent net drain on the player's wallet.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Faction standing → tier mapping (docs/faction system.pdf §Standing Tiers)
# ---------------------------------------------------------------------------


def faction_tier(standing: int) -> str:
    """Map a numeric faction standing (-100..100) to a named tier.

    Thresholds from docs/faction system.pdf:
      hostile: -100 to -51, unfriendly: -50 to -1, neutral: 0,
      friendly: 1 to 50, allied: 51 to 100.
    """
    standing = max(-100, min(100, standing))
    if standing >= 51:
        return "allied"
    if standing >= 1:
        return "friendly"
    if standing == 0:
        return "neutral"
    if standing >= -50:
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

# Faction tier → buy price multiplier (additive: final_mult = 1.0 + value)
# hostile +25% from doc but shop refuses; unfriendly +25%; friendly -10%; allied -20%
_FACTION_BUY_MODIFIER: dict[str, float] = {
    "allied": -0.20,
    "friendly": -0.10,
    "neutral": 0.0,
    "unfriendly": 0.25,
    "hostile": 0.0,  # shop refuses
}

DEFAULT_SELL_BACK_RATIO = 0.50


def _resolve_tier(
    standing: int,
    reputation_thresholds: dict[str, int] | None,
) -> str:
    """Resolve tier using custom thresholds when available, else defaults."""
    if reputation_thresholds is None:
        return faction_tier(standing)
    # Lazy import to avoid circular dependency (reputation → pricing → reputation)
    from relay.factions.reputation import resolve_tier

    return resolve_tier(standing, reputation_thresholds)


def compute_buy_price(
    *,
    base_value: int,
    markup_pct: float,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
    reputation_thresholds: dict[str, int] | None = None,
    shop_price_modifiers: dict[str, float] | None = None,
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
    reputation_thresholds : dict[str, int] | None
        Per-faction custom tier boundaries. When provided, uses these instead
        of the hardcoded defaults in :func:`faction_tier`.
    shop_price_modifiers : dict[str, float] | None
        Per-faction buy-price multipliers (tier → multiplier). When provided,
        uses these instead of the global ``_FACTION_BUY_MODIFIER`` table.
        Values are direct multipliers: 0.80 means "pay 80% of base+markup".

    Returns
    -------
    int
        Final price (always ≥ 1).
    """
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    tier = _resolve_tier(standing, reputation_thresholds)

    markup_mult = 1.0 + (markup_pct / 100.0)

    if shop_price_modifiers and tier in shop_price_modifiers:
        faction_mult = shop_price_modifiers[tier]
    else:
        faction_mult = 1.0 + _FACTION_BUY_MODIFIER[tier]

    price = base_value * markup_mult * faction_mult
    return max(1, round(price))


def compute_sell_price(
    *,
    base_value: int,
    sell_back_ratio: float = DEFAULT_SELL_BACK_RATIO,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
    reputation_thresholds: dict[str, int] | None = None,
) -> int:
    """Compute the sell price for an item.

    Parameters
    ----------
    reputation_thresholds : dict[str, int] | None
        Per-faction custom tier boundaries. See :func:`compute_buy_price`.

    Returns
    -------
    int
        Final sell price (always ≥ 0 — 0 means the item has no sell value).
    """
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    tier = _resolve_tier(standing, reputation_thresholds)

    effective_ratio = sell_back_ratio + _FACTION_SELL_MODIFIER[tier]
    effective_ratio = max(0.0, min(1.0, effective_ratio))
    return max(0, math.floor(base_value * effective_ratio))


def is_hostile(
    *,
    faction_standing: int | None = None,
    faction_id: str | None = None,
    character_faction_standing: dict[str, int] | None = None,
    reputation_thresholds: dict[str, int] | None = None,
) -> bool:
    """Return True if the faction standing is hostile (shop refuses trade)."""
    standing = _resolve_standing(faction_standing, faction_id, character_faction_standing)
    return _resolve_tier(standing, reputation_thresholds) == "hostile"


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
