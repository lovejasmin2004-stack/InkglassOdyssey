"""Shop endpoints — browse, buy, sell.

Invariant #14: all economy transactions through relay endpoints.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
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
    _extract_price_modifiers,
    _extract_thresholds,
    _load_faction_data,
    _npc_faction_id,
    buy_item,
    get_shop_prices,
    sell_item,
)
from relay.economy.wallet import InsufficientFunds
from relay.endpoints._helpers import load_character_owned
from relay.schemas import Item, NpcPersonality
from relay.world.item_loader import load_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shop", tags=["economy"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]


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
# Helpers
# ---------------------------------------------------------------------------


async def _load_shop_items(npc: NpcPersonality, world_id: str) -> dict[str, Item]:
    """Load all items referenced by a shop NPC (concurrent)."""
    if npc.shop_data is None:
        return {}
    item_ids = [entry.item_id for entry in npc.shop_data.inventory]
    results = await asyncio.gather(*[load_item(iid, world_id) for iid in item_ids])
    return {iid: item for iid, item in zip(item_ids, results, strict=True) if item is not None}


async def _load_npc_or_404(npc_id: str) -> NpcPersonality:
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


def _check_access_prerequisites(npc: NpcPersonality, character) -> None:
    """Enforce shop access_prerequisites if defined."""
    prereqs = npc.shop_data and npc.shop_data.access_prerequisites
    if prereqs is None:
        return

    if prereqs.level_requirement is not None and character.level < prereqs.level_requirement:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "level_too_low",
                "message": f"Shop requires level {prereqs.level_requirement}",
            },
        )

    if prereqs.faction_standing_threshold is not None:
        faction_id = npc.faction_id
        if faction_id is None and npc.world_position:
            faction_id = npc.world_position.region_id
        standings = character.faction_standing or {}
        standing = standings.get(faction_id, 0) if faction_id else 0
        if standing < prereqs.faction_standing_threshold:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "faction_too_low",
                    "message": "Your faction standing is too low to access this shop",
                },
            )


def _check_world_match(npc: NpcPersonality, character) -> None:
    """Ensure character and NPC belong to the same world."""
    if npc.world_id != character.world_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "world_mismatch",
                "message": f"Character world '{character.world_id}' does not match NPC world '{npc.world_id}'",
            },
        )


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
    _check_world_match(npc, character)
    _check_access_prerequisites(npc, character)
    items = await _load_shop_items(npc, character.world_id)

    # Load faction data for custom tier thresholds and price modifiers
    faction_id = _npc_faction_id(npc)
    faction_def = await _load_faction_data(faction_id, character.world_id)
    thresholds = _extract_thresholds(faction_def)
    price_modifiers = _extract_price_modifiers(faction_def)

    quotes = get_shop_prices(
        npc=npc,
        items=items,
        character=character,
        reputation_thresholds=thresholds,
        shop_price_modifiers=price_modifiers,
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
    _check_world_match(npc, character)
    _check_access_prerequisites(npc, character)
    item = await load_item(body.item_id, character.world_id)

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

    await db.commit()
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
    _check_world_match(npc, character)
    item = await load_item(body.item_id, character.world_id)

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

    await db.commit()
    return TransactionReceipt(**receipt)
