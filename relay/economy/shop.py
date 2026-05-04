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

from sqlalchemy.ext.asyncio import AsyncSession

from relay.economy.pricing import (
    _DEFAULT_SELL_BACK_RATIO,
    compute_buy_price,
    compute_sell_price,
    faction_tier,
    is_hostile,
)
from relay.economy.wallet import InsufficientFunds, credit, debit
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
    sell_back_ratio: float = _DEFAULT_SELL_BACK_RATIO,
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
        )
        sell_price = compute_sell_price(
            base_value=item.value,
            sell_back_ratio=sell_back_ratio,
            faction_standing=standing,
        )

        quotes.append(PriceQuote(
            item_id=item.id,
            item_name=item.name,
            base_value=item.value,
            markup_pct=entry.markup_percentage,
            buy_price=buy_price,
            sell_price=sell_price,
            stock=entry.stock_quantity,
            faction_tier=tier,
        ))

    return quotes


async def buy_item(
    db: AsyncSession,
    *,
    character: Character,
    npc: NpcPersonality,
    item: Item,
    quantity: int = 1,
    sell_back_ratio: float = _DEFAULT_SELL_BACK_RATIO,
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

    # Check faction
    faction_id = _npc_faction_id(npc)
    standings = character.faction_standing or {}
    standing = standings.get(faction_id, 0) if faction_id else 0

    if is_hostile(faction_standing=standing):
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
    tier = faction_tier(standing)
    unit_price = compute_buy_price(
        base_value=item.value,
        markup_pct=shop_entry.markup_percentage,
        faction_standing=standing,
    )
    total_price = unit_price * quantity

    # Debit wallet
    currency = character.world_id
    new_balance = await debit(
        db, character,
        currency=currency,
        amount=total_price,
        tx_type="buy",
        item_id=item.id,
        item_quantity=quantity,
        npc_id=npc.id,
        base_price=item.value,
        markup_pct=shop_entry.markup_percentage,
        faction_modifier=standing,
        note=f"Bought {quantity}x {item.name} from {npc.name}",
    )

    # Add to character inventory
    inventory = list(character.inventory or [])
    _add_to_inventory(inventory, item, quantity)
    character.inventory = inventory

    # Decrement shop stock (in-memory only — NPC files are static content,
    # runtime stock tracking will use a DB table in Phase 2)
    # For now we trust the stock_quantity in the NPC file.

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
    sell_back_ratio: float = _DEFAULT_SELL_BACK_RATIO,
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

    # Check faction
    faction_id = _npc_faction_id(npc)
    standings = character.faction_standing or {}
    standing = standings.get(faction_id, 0) if faction_id else 0

    if is_hostile(faction_standing=standing):
        raise HostileFaction(f"NPC '{npc.id}' refuses to trade (hostile faction)")

    # Check character has the item
    inventory = list(character.inventory or [])
    inv_entry = _find_inventory_entry(inventory, item.id)
    if inv_entry is None or inv_entry.get("quantity", 0) < quantity:
        raise ItemNotInInventory(
            f"Character does not have {quantity}x '{item.id}'"
        )

    # Check binding
    if inv_entry.get("binding_state") == "bound":
        raise BoundItemCannotSell(f"Item '{item.id}' is bound and cannot be sold")

    # Compute sell price
    tier = faction_tier(standing)
    unit_price = compute_sell_price(
        base_value=item.value,
        sell_back_ratio=sell_back_ratio,
        faction_standing=standing,
    )
    total_price = unit_price * quantity

    # Credit wallet
    currency = character.world_id
    new_balance = await credit(
        db, character,
        currency=currency,
        amount=total_price,
        tx_type="sell",
        item_id=item.id,
        item_quantity=quantity,
        npc_id=npc.id,
        base_price=item.value,
        sell_back_ratio=sell_back_ratio,
        faction_modifier=standing,
        note=f"Sold {quantity}x {item.name} to {npc.name}",
    )

    # Remove from inventory
    _remove_from_inventory(inventory, item.id, quantity)
    character.inventory = inventory

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

    Convention: the NPC's world_position.region_id is used as a faction key.
    If the NPC has no world_position, returns None (neutral pricing).
    """
    if npc.world_position:
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
    """Add an item to the inventory list, stacking if possible."""
    for entry in inventory:
        if entry.get("item_id") == item.id:
            entry["quantity"] = entry.get("quantity", 1) + quantity
            return

    inventory.append({
        "item_id": item.id,
        "quantity": quantity,
        "binding_state": "bound" if item.binding == "bind_on_acquire" else "unbound",
    })


def _find_inventory_entry(inventory: list[dict], item_id: str) -> dict | None:
    for entry in inventory:
        if entry.get("item_id") == item_id:
            return entry
    return None


def _remove_from_inventory(inventory: list[dict], item_id: str, quantity: int) -> None:
    """Remove quantity of an item from inventory. Removes entry if quantity hits 0."""
    for i, entry in enumerate(inventory):
        if entry.get("item_id") == item_id:
            remaining = entry.get("quantity", 1) - quantity
            if remaining <= 0:
                inventory.pop(i)
            else:
                entry["quantity"] = remaining
            return
