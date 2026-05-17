"""Shop endpoints — browse, buy, sell.

Invariant #14: all economy transactions through relay endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from relay.ai.npc_loader import NpcLoadError, load_npc
from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.economy.shop import (
    BoundItemCannotSell,
    HostileFaction,
    ItemNotInInventory,
    ItemNotInShop,
    LegendaryCannotPurchase,
    OutOfStock,
    buy_item,
    get_shop_prices,
    sell_item,
)
from relay.economy.wallet import InsufficientFunds
from relay.endpoints._helpers import load_character_owned
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
    total: int
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

_MAX_ITEM_CACHE_SIZE = 512
_item_cache: OrderedDict[str, Item] = OrderedDict()
_item_lock = asyncio.Lock()


async def invalidate_item(item_id: str, world_id: str) -> None:
    """Remove an item from cache so the next load reads fresh from disk."""
    async with _item_lock:
        _item_cache.pop(f"{world_id}:{item_id}", None)
        _item_cache.pop(item_id, None)


async def invalidate_item_cache_for_world(world_id: str) -> None:
    """Remove all cached items for a world."""
    prefix = f"{world_id}:"
    async with _item_lock:
        keys = [k for k in _item_cache if k.startswith(prefix)]
        for k in keys:
            del _item_cache[k]


def _read_and_parse_item(path: Path) -> Item | None:
    """Blocking I/O — run via to_thread."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return Item.model_validate(data)


def _cache_put(key: str, item: Item) -> None:
    """Insert into cache with LRU eviction. Must be called under _item_lock."""
    _item_cache[key] = item
    _item_cache.move_to_end(key)
    while len(_item_cache) > _MAX_ITEM_CACHE_SIZE:
        _item_cache.popitem(last=False)


async def _load_item(item_id: str, world_id: str | None = None) -> Item | None:
    """Load an item definition from disk."""
    cache_key = f"{world_id}:{item_id}" if world_id else item_id
    async with _item_lock:
        if cache_key in _item_cache:
            _item_cache.move_to_end(cache_key)
            return _item_cache[cache_key]

    if world_id:
        path = _ITEMS_ROOT / world_id / f"{item_id}.json"
        if path.exists():
            item = await _parse_item(path)
            if item:
                async with _item_lock:
                    _cache_put(cache_key, item)
                return item

    for path in _ITEMS_ROOT.rglob(f"{item_id}.json"):
        item = await _parse_item(path)
        if item:
            resolved_key = f"{path.parent.name}:{item_id}"
            async with _item_lock:
                _cache_put(resolved_key, item)
            return item

    return None


async def _parse_item(path: Path) -> Item | None:
    try:
        return await asyncio.to_thread(_read_and_parse_item, path)
    except (json.JSONDecodeError, OSError, ValidationError):
        logger.warning("Failed to load item", extra={"path": str(path)})
        return None


async def _load_shop_items(npc, world_id: str | None = None) -> dict[str, Item]:
    """Load all items referenced by a shop NPC (concurrent)."""
    if npc.shop_data is None:
        return {}
    item_ids = [entry.item_id for entry in npc.shop_data.inventory]
    results = await asyncio.gather(*[_load_item(iid, world_id) for iid in item_ids])
    return {iid: item for iid, item in zip(item_ids, results, strict=True) if item is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_npc_or_404(npc_id: str):
    try:
        npc = await load_npc(npc_id)
    except NpcLoadError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "npc_load_error", "message": str(exc)},
        ) from None
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
    limit: int = Query(default=50, ge=1, le=200, description="Max items per page"),
    offset: int = Query(default=0, ge=0, description="Items to skip"),
) -> ShopResponse:
    """Browse a shop's inventory with prices adjusted for faction standing."""
    character = await load_character_owned(db, character_id, token.player_id)
    npc = await _load_npc_or_404(npc_id)
    items = await _load_shop_items(npc, character.world_id)

    quotes = get_shop_prices(
        npc=npc,
        items=items,
        character=character,
    )

    total = len(quotes)
    tier = quotes[0].faction_tier if quotes else "neutral"
    page = quotes[offset : offset + limit]

    return ShopResponse(
        npc_id=npc.id,
        npc_name=npc.name,
        faction_tier=tier,
        total=total,
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
            for q in page
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
    character = await load_character_owned(db, body.character_id, token.player_id)
    npc = await _load_npc_or_404(npc_id)
    item = await _load_item(body.item_id, character.world_id)

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
        ) from None
    except LegendaryCannotPurchase:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "legendary_cannot_purchase", "message": "Legendary items cannot be purchased"},
        ) from None
    except ItemNotInShop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "item_not_in_shop", "message": f"Item '{body.item_id}' not in this shop"},
        ) from None
    except OutOfStock:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "out_of_stock", "message": "Shop is out of stock for this item"},
        ) from None
    except InsufficientFunds as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "insufficient_funds", "message": str(exc)},
        ) from None

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
    character = await load_character_owned(db, body.character_id, token.player_id)
    npc = await _load_npc_or_404(npc_id)
    item = await _load_item(body.item_id, character.world_id)

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
        ) from None
    except ItemNotInInventory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "item_not_in_inventory", "message": "You don't have enough of that item"},
        ) from None
    except BoundItemCannotSell:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bound_item", "message": "Bound items cannot be sold"},
        ) from None

    await db.flush()
    return TransactionReceipt(**receipt)
