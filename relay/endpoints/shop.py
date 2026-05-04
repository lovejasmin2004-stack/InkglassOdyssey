"""Shop endpoints — browse, buy, sell.

Invariant #14: all economy transactions through relay endpoints.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.ai.npc_loader import NpcLoadError, load_npc
from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.economy.pricing import is_hostile
from relay.economy.shop import (
    BoundItemCannotSell,
    HostileFaction,
    ItemNotInInventory,
    ItemNotInShop,
    LegendaryCannotPurchase,
    OutOfStock,
    PriceQuote,
    get_shop_prices,
    buy_item,
    sell_item,
)
from relay.economy.wallet import InsufficientFunds
from relay.models import Character
from relay.schemas import Item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shop", tags=["economy"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]

_ITEMS_ROOT = Path(__file__).parents[2] / "items"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ShopItemResponse(BaseModel):
    item_id: str
    item_name: str
    base_value: int
    markup_pct: float
    buy_price: int
    sell_price: int
    stock: int
    faction_tier: str


class ShopResponse(BaseModel):
    npc_id: str
    npc_name: str
    faction_tier: str
    items: list[ShopItemResponse]


class BuySellRequest(BaseModel):
    character_id: str
    item_id: str
    quantity: int = Field(ge=1, default=1)

    model_config = {"extra": "forbid"}


class TransactionReceipt(BaseModel):
    tx_type: str
    item_id: str
    item_name: str
    quantity: int
    unit_price: int
    total_price: int
    currency: str
    balance_after: int
    faction_tier: str


# ---------------------------------------------------------------------------
# Item loader
# ---------------------------------------------------------------------------

_item_cache: dict[str, Item] = {}


def _load_item(item_id: str, world_id: str | None = None) -> Item | None:
    """Load an item definition from disk."""
    cache_key = f"{world_id}:{item_id}" if world_id else item_id
    if cache_key in _item_cache:
        return _item_cache[cache_key]

    if world_id:
        path = _ITEMS_ROOT / world_id / f"{item_id}.json"
        if path.exists():
            item = _parse_item(path)
            if item:
                _item_cache[cache_key] = item
                return item

    # Search all worlds
    for path in _ITEMS_ROOT.rglob(f"{item_id}.json"):
        item = _parse_item(path)
        if item:
            resolved_key = f"{path.parent.name}:{item_id}"
            _item_cache[resolved_key] = item
            return item

    return None


def _parse_item(path: Path) -> Item | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Item.model_validate(data)
    except Exception:
        logger.warning("Failed to load item", extra={"path": str(path)})
        return None


def _load_shop_items(npc, world_id: str | None = None) -> dict[str, Item]:
    """Load all items referenced by a shop NPC."""
    if npc.shop_data is None:
        return {}
    items: dict[str, Item] = {}
    for entry in npc.shop_data.inventory:
        item = _load_item(entry.item_id, world_id)
        if item:
            items[entry.item_id] = item
    return items


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_character(db: AsyncSession, character_id: str, player_id: str) -> Character:
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Character not found"},
        )
    if character.player_id != player_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Character belongs to another player"},
        )
    return character


def _load_npc_or_404(npc_id: str):
    try:
        npc = load_npc(npc_id)
    except NpcLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "npc_load_error", "message": str(exc)},
        )
    if npc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"NPC '{npc_id}' not found"},
        )
    if npc.shop_data is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "not_a_shop", "message": f"NPC '{npc_id}' is not a shop"},
        )
    return npc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{npc_id}", response_model=ShopResponse)
async def get_shop(
    npc_id: str,
    character_id: str,
    token: Token,
    db: DB,
) -> ShopResponse:
    """Browse a shop's inventory with prices adjusted for faction standing."""
    character = await _get_character(db, character_id, token.player_id)
    npc = _load_npc_or_404(npc_id)
    items = _load_shop_items(npc, character.world_id)

    quotes = get_shop_prices(
        npc=npc,
        items=items,
        character=character,
    )

    tier = quotes[0].faction_tier if quotes else "neutral"

    return ShopResponse(
        npc_id=npc.id,
        npc_name=npc.name,
        faction_tier=tier,
        items=[
            ShopItemResponse(
                item_id=q.item_id,
                item_name=q.item_name,
                base_value=q.base_value,
                markup_pct=q.markup_pct,
                buy_price=q.buy_price,
                sell_price=q.sell_price,
                stock=q.stock,
                faction_tier=q.faction_tier,
            )
            for q in quotes
        ],
    )


@router.post("/{npc_id}/buy", response_model=TransactionReceipt)
async def buy_from_shop(
    npc_id: str,
    body: BuySellRequest,
    token: Token,
    db: DB,
) -> TransactionReceipt:
    """Buy an item from a shop NPC."""
    character = await _get_character(db, body.character_id, token.player_id)
    npc = _load_npc_or_404(npc_id)
    item = _load_item(body.item_id, character.world_id)

    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Item '{body.item_id}' not found"},
        )

    try:
        receipt = await buy_item(
            db,
            character=character,
            npc=npc,
            item=item,
            quantity=body.quantity,
        )
    except HostileFaction:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "hostile_faction", "message": "Shop refuses to trade with you"},
        )
    except LegendaryCannotPurchase:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "legendary_cannot_purchase", "message": "Legendary items cannot be purchased"},
        )
    except ItemNotInShop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "item_not_in_shop", "message": f"Item '{body.item_id}' not in this shop"},
        )
    except OutOfStock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "out_of_stock", "message": "Shop is out of stock for this item"},
        )
    except InsufficientFunds as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "insufficient_funds", "message": str(exc)},
        )

    await db.flush()
    return TransactionReceipt(**receipt)


@router.post("/{npc_id}/sell", response_model=TransactionReceipt)
async def sell_to_shop(
    npc_id: str,
    body: BuySellRequest,
    token: Token,
    db: DB,
) -> TransactionReceipt:
    """Sell an item to a shop NPC."""
    character = await _get_character(db, body.character_id, token.player_id)
    npc = _load_npc_or_404(npc_id)
    item = _load_item(body.item_id, character.world_id)

    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Item '{body.item_id}' not found"},
        )

    try:
        receipt = await sell_item(
            db,
            character=character,
            npc=npc,
            item=item,
            quantity=body.quantity,
        )
    except HostileFaction:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "hostile_faction", "message": "Shop refuses to trade with you"},
        )
    except ItemNotInInventory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "item_not_in_inventory", "message": "You don't have enough of that item"},
        )
    except BoundItemCannotSell:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bound_item", "message": "Bound items cannot be sold"},
        )

    await db.flush()
    return TransactionReceipt(**receipt)
