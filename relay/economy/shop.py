"""Shop buy/sell operations.

Loads shop inventory from NPC personality files (shop_data field),
computes prices using the pricing engine, and executes transactions
against the character's wallet and inventory.

Invariant #14: all economy transactions through relay endpoints.
Invariant #8: LLM is never authoritative over mechanical state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.economy.pricing import (
    DEFAULT_SELL_BACK_RATIO,
    compute_buy_price,
    compute_sell_price,
    faction_tier,
    is_hostile,
)
from relay.economy.wallet import credit, debit
from relay.models import Character
from relay.schemas import Item, NpcPersonality, ShopInventoryEntry

logger = logging.getLogger(__name__)


class ShopError(Exception):
    """Base for shop-specific errors."""


class HostileFaction(ShopError):
    """The shop NPC's faction refuses to trade with this character."""


class ItemNotInShop(ShopError):
    """The requested item is not in this shop's inventory."""


class OutOfStock(ShopError):
    """The shop has zero stock of the requested item."""


class LegendaryCannotPurchase(ShopError):
    """Legendary items cannot be purchased from shops."""


class ItemNotInInventory(ShopError):
    """The character doesn't have the item to sell."""


class BoundItemCannotSell(ShopError):
    """Bound items cannot be sold."""


# ---------------------------------------------------------------------------
# World → currency mapping
# ---------------------------------------------------------------------------

# Each world has its own currency name for display purposes.
# Keys are world_ids, values are canonical currency names.
_WORLD_CURRENCIES: dict[str, str] = {
    "inkglass_dark": "gold",
    "murim": "silver_taels",
    "cybernightlife": "credits",
    "wha_au": "sintar",
    "atla_au": "yuan",
    "gachiakuta_au": "scrip",
    "hxh_au": "jenny",
}


def get_world_currency(world_id: str) -> str:
    """Return the canonical currency key for a world.

    Falls back to the world_id itself if no mapping is defined, ensuring
    forward-compatibility with new worlds.
    """
    return _WORLD_CURRENCIES.get(world_id, world_id)


@dataclass
class PriceQuote:
    """Price information for a shop item."""

    item_id: str
    item_name: str
    base_value: int
    markup_pct: float
    buy_price: int
    sell_price: int
    stock: int
    faction_tier: str


def get_shop_prices(
    *,
    npc: NpcPersonality,
    items: dict[str, Item],
    character: Character,
    sell_back_ratio: float = DEFAULT_SELL_BACK_RATIO,
    reputation_thresholds: dict[str, int] | None = None,
    shop_price_modifiers: dict[str, float] | None = None,
) -> list[PriceQuote]:
    """Compute buy/sell prices for all items in a shop NPC's inventory.

    Parameters
    ----------
    npc : NpcPersonality
        The shop NPC (must have ``shop_data``).
    items : dict[str, Item]
        Map of item_id → Item for all items referenced by the shop.
    character : Character
        The buying character (used for faction standing lookup).
    sell_back_ratio : float
        World-configured sell-back ratio (default 0.50).
    reputation_thresholds : dict[str, int] | None
        Per-faction custom tier boundaries from the faction definition.
    shop_price_modifiers : dict[str, float] | None
        Per-faction buy-price multipliers (tier → multiplier).

    Returns
    -------
    list[PriceQuote]
        Price quotes for every item in the shop.
    """
    if npc.shop_data is None:
        return []

    faction_id = _npc_faction_id(npc)
    standings = character.faction_standing or {}
    standing = standings.get(faction_id, 0) if faction_id else 0

    if reputation_thresholds:
        from relay.factions.reputation import resolve_tier

        tier = resolve_tier(standing, reputation_thresholds)
    else:
        tier = faction_tier(standing)

    quotes: list[PriceQuote] = []
    for entry in npc.shop_data.inventory:
        item = items.get(entry.item_id)
        if item is None:
            logger.warning(
                "Shop references unknown item",
                extra={"npc_id": npc.id, "item_id": entry.item_id},
            )
            continue

        buy_price = compute_buy_price(
            base_value=item.value,
            markup_pct=entry.markup_percentage,
            faction_standing=standing,
            reputation_thresholds=reputation_thresholds,
            shop_price_modifiers=shop_price_modifiers,
        )
        sell_price = compute_sell_price(
            base_value=item.value,
            sell_back_ratio=sell_back_ratio,
            faction_standing=standing,
            reputation_thresholds=reputation_thresholds,
        )

        quotes.append(
            PriceQuote(
                item_id=item.id,
                item_name=item.name,
                base_value=item.value,
                markup_pct=entry.markup_percentage,
                buy_price=buy_price,
                sell_price=sell_price,
                stock=entry.stock_quantity,
                faction_tier=tier,
            )
        )

    return quotes


async def _load_faction_data(
    faction_id: str | None, world_id: str
) -> dict[str, Any] | None:
    """Load faction definition for pricing context.

    Returns None if the faction cannot be resolved.
    """
    if not faction_id:
        return None
    from relay.world.content_loader import load_faction

    return await load_faction(faction_id, world_id)


def _extract_thresholds(faction_def: dict[str, Any] | None) -> dict[str, int] | None:
    """Extract reputation_thresholds from a loaded faction definition."""
    if faction_def is None:
        return None
    return faction_def.get("reputation_thresholds")


def _extract_price_modifiers(faction_def: dict[str, Any] | None) -> dict[str, float] | None:
    """Extract shop_price_modifiers from a loaded faction definition."""
    if faction_def is None:
        return None
    mods = faction_def.get("shop_price_modifiers")
    if mods is None:
        return None
    # Convert from nested Pydantic-style to flat {tier: multiplier}
    return {k: v for k, v in mods.items() if v is not None}


async def buy_item(
    db: AsyncSession,
    *,
    character: Character,
    npc: NpcPersonality,
    item: Item,
    quantity: int = 1,
    sell_back_ratio: float = DEFAULT_SELL_BACK_RATIO,
) -> dict:
    """Execute a buy transaction: debit wallet, add to inventory, log.

    Returns a receipt dict with transaction details.

    Raises
    ------
    HostileFaction
        If the NPC's faction is hostile to the character.
    ItemNotInShop
        If the item isn't in this NPC's shop_data.
    OutOfStock
        If the shop has zero stock.
    LegendaryCannotPurchase
        If the item is legendary rarity.
    InsufficientFunds
        If the character can't afford it.
    """
    if npc.shop_data is None:
        raise ItemNotInShop(f"NPC '{npc.id}' is not a shop")

    # Load faction data for custom thresholds and price modifiers
    faction_id = _npc_faction_id(npc)
    faction_def = await _load_faction_data(faction_id, character.world_id)
    thresholds = _extract_thresholds(faction_def)
    price_modifiers = _extract_price_modifiers(faction_def)

    standings = character.faction_standing or {}
    standing = standings.get(faction_id, 0) if faction_id else 0

    if is_hostile(faction_standing=standing, reputation_thresholds=thresholds):
        raise HostileFaction(f"NPC '{npc.id}' refuses to trade (hostile faction)")

    # Legendary cannot be purchased
    if item.rarity == "legendary":
        raise LegendaryCannotPurchase(f"Legendary item '{item.id}' cannot be purchased")

    # Find item in shop inventory
    shop_entry = _find_shop_entry(npc, item.id)
    if shop_entry is None:
        raise ItemNotInShop(f"Item '{item.id}' not in shop '{npc.id}'")

    if shop_entry.stock_quantity < quantity:
        raise OutOfStock(f"Shop has {shop_entry.stock_quantity} of '{item.id}', requested {quantity}")

    # Compute price
    if thresholds:
        from relay.factions.reputation import resolve_tier

        tier = resolve_tier(standing, thresholds)
    else:
        tier = faction_tier(standing)

    unit_price = compute_buy_price(
        base_value=item.value,
        markup_pct=shop_entry.markup_percentage,
        faction_standing=standing,
        reputation_thresholds=thresholds,
        shop_price_modifiers=price_modifiers,
    )
    total_price = unit_price * quantity

    # Compute the actual faction multiplier for the audit trail
    if price_modifiers and tier in price_modifiers:
        faction_mult = price_modifiers[tier]
    else:
        from relay.economy.pricing import _FACTION_BUY_MODIFIER

        faction_mult = 1.0 + _FACTION_BUY_MODIFIER[tier]

    # Debit wallet
    currency = get_world_currency(character.world_id)
    new_balance = await debit(
        db,
        character,
        currency=currency,
        amount=total_price,
        tx_type="buy",
        item_id=item.id,
        item_quantity=quantity,
        npc_id=npc.id,
        base_price=item.value,
        markup_pct=shop_entry.markup_percentage,
        faction_modifier=faction_mult,
        sell_back_ratio=sell_back_ratio,
        note=f"Bought {quantity}x {item.name} from {npc.name}",
    )

    # Add to character inventory
    inventory = list(character.inventory or [])
    _add_to_inventory(inventory, item, quantity)
    character.inventory = inventory
    flag_modified(character, "inventory")

    logger.info(
        "Buy transaction complete",
        extra={
            "character_id": character.id,
            "item_id": item.id,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_price": total_price,
            "balance_after": new_balance,
        },
    )

    return {
        "tx_type": "buy",
        "item_id": item.id,
        "item_name": item.name,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "currency": currency,
        "balance_after": new_balance,
        "faction_tier": tier,
    }


async def sell_item(
    db: AsyncSession,
    *,
    character: Character,
    npc: NpcPersonality,
    item: Item,
    quantity: int = 1,
    sell_back_ratio: float = DEFAULT_SELL_BACK_RATIO,
) -> dict:
    """Execute a sell transaction: credit wallet, remove from inventory, log.

    Returns a receipt dict with transaction details.

    Raises
    ------
    HostileFaction
        If the NPC's faction is hostile to the character.
    ItemNotInInventory
        If the character doesn't have enough of the item.
    BoundItemCannotSell
        If the item in inventory is bound to the character.
    """
    if npc.shop_data is None:
        raise ShopError(f"NPC '{npc.id}' is not a shop")

    # Load faction data for custom thresholds
    faction_id = _npc_faction_id(npc)
    faction_def = await _load_faction_data(faction_id, character.world_id)
    thresholds = _extract_thresholds(faction_def)

    standings = character.faction_standing or {}
    standing = standings.get(faction_id, 0) if faction_id else 0

    if is_hostile(faction_standing=standing, reputation_thresholds=thresholds):
        raise HostileFaction(f"NPC '{npc.id}' refuses to trade (hostile faction)")

    # Check character has the item (prefer unbound stacks for selling)
    inventory = list(character.inventory or [])
    inv_entry = _find_inventory_entry(inventory, item.id, prefer_unbound=True)
    if inv_entry is None or inv_entry.get("quantity", 0) < quantity:
        raise ItemNotInInventory(f"Character does not have {quantity}x '{item.id}'")

    # Check binding
    # TODO(Phase 2): When equip endpoints are implemented, bind_on_equip items
    # must have their binding_state set to "bound" at equip time.  The check
    # below already handles that case correctly — as long as the equip logic
    # writes binding_state="bound", this guard will block selling.
    if inv_entry.get("binding_state") == "bound":
        raise BoundItemCannotSell(f"Item '{item.id}' is bound and cannot be sold")

    # Compute sell price
    if thresholds:
        from relay.factions.reputation import resolve_tier

        tier = resolve_tier(standing, thresholds)
    else:
        tier = faction_tier(standing)

    unit_price = compute_sell_price(
        base_value=item.value,
        sell_back_ratio=sell_back_ratio,
        faction_standing=standing,
        reputation_thresholds=thresholds,
    )
    total_price = unit_price * quantity

    # Compute effective sell-back ratio for audit trail
    from relay.economy.pricing import _FACTION_SELL_MODIFIER

    effective_sell_ratio = sell_back_ratio + _FACTION_SELL_MODIFIER.get(tier, 0.0)
    effective_sell_ratio = max(0.0, min(1.0, effective_sell_ratio))

    # Credit wallet
    currency = get_world_currency(character.world_id)
    new_balance = await credit(
        db,
        character,
        currency=currency,
        amount=total_price,
        tx_type="sell",
        item_id=item.id,
        item_quantity=quantity,
        npc_id=npc.id,
        base_price=item.value,
        sell_back_ratio=effective_sell_ratio,
        faction_modifier=effective_sell_ratio,
        note=f"Sold {quantity}x {item.name} to {npc.name}",
    )

    # Remove from inventory
    _remove_from_inventory(inventory, item.id, quantity)
    character.inventory = inventory
    flag_modified(character, "inventory")

    logger.info(
        "Sell transaction complete",
        extra={
            "character_id": character.id,
            "item_id": item.id,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_price": total_price,
            "balance_after": new_balance,
        },
    )

    return {
        "tx_type": "sell",
        "item_id": item.id,
        "item_name": item.name,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "currency": currency,
        "balance_after": new_balance,
        "faction_tier": tier,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _npc_faction_id(npc: NpcPersonality) -> str | None:
    """Derive the faction ID for an NPC.

    Checks for an explicit ``faction_id`` on the NPC first (from npc_personality
    schema).  Falls back to ``world_position.region_id`` as a convention.
    Returns None (neutral pricing) if neither is available.
    """
    if npc.faction_id:
        return npc.faction_id
    if npc.world_position:
        logger.debug(
            "NPC lacks explicit faction_id — falling back to region_id for pricing",
            extra={"npc_id": npc.id, "region_id": npc.world_position.region_id},
        )
        return npc.world_position.region_id
    return None


def _find_shop_entry(npc: NpcPersonality, item_id: str) -> ShopInventoryEntry | None:
    if npc.shop_data is None:
        return None
    for entry in npc.shop_data.inventory:
        if entry.item_id == item_id:
            return entry
    return None


def _add_to_inventory(inventory: list[dict], item: Item, quantity: int) -> None:
    """Add an item to the inventory list, stacking if binding state matches.

    (#4) Stacking now matches on both item_id *and* binding_state to prevent
    merging unbound purchases into an existing bound stack (or vice versa).
    """
    new_binding = "bound" if item.binding == "bind_on_acquire" else "unbound"
    for entry in inventory:
        if entry.get("item_id") == item.id and entry.get("binding_state") == new_binding:
            entry["quantity"] = entry.get("quantity", 1) + quantity
            return

    inventory.append(
        {
            "item_id": item.id,
            "quantity": quantity,
            "binding_state": new_binding,
        }
    )


def _find_inventory_entry(
    inventory: list[dict], item_id: str, *, prefer_unbound: bool = False
) -> dict | None:
    """Find an inventory entry by item_id.

    When prefer_unbound is True (used for selling), returns an unbound stack
    first so that selling isn't blocked by a bound stack appearing earlier.
    """
    fallback: dict | None = None
    for entry in inventory:
        if entry.get("item_id") == item_id:
            if not prefer_unbound:
                return entry
            if entry.get("binding_state") != "bound":
                return entry
            if fallback is None:
                fallback = entry
    return fallback


def _remove_from_inventory(inventory: list[dict], item_id: str, quantity: int) -> None:
    """Remove quantity of an item from inventory. Removes entry if quantity hits 0.

    Prefers unbound stacks so selling doesn't accidentally target bound ones.
    """
    target_idx: int | None = None
    for i, entry in enumerate(inventory):
        if entry.get("item_id") == item_id:
            if entry.get("binding_state") != "bound":
                target_idx = i
                break
            if target_idx is None:
                target_idx = i

    if target_idx is not None:
        entry = inventory[target_idx]
        remaining = entry.get("quantity", 1) - quantity
        if remaining <= 0:
            inventory.pop(target_idx)
        else:
            entry["quantity"] = remaining
