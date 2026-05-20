"""Wallet endpoints — balance query and admin grant.

Invariant #14: all economy transactions through relay endpoints.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.economy.wallet import credit
from relay.endpoints._helpers import load_character_owned
from relay.models import TransactionLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wallet", tags=["economy"])

Token = Annotated[AccountTokenPayload | SessionTokenPayload, Depends(get_current_token)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class WalletResponse(BaseModel):
    character_id: str
    world_id: str
    balances: dict[str, int]


class GrantRequest(BaseModel):
    character_id: str
    currency: str
    amount: int = Field(ge=1)
    note: str | None = None

    model_config = {"extra": "forbid"}


class GrantResponse(BaseModel):
    character_id: str
    currency: str
    amount: int
    balance_after: int


class TransactionEntry(BaseModel):
    id: str
    tx_type: str
    amount: int
    currency: str
    balance_after: int
    item_id: str | None
    item_quantity: int | None
    npc_id: str | None
    session_id: str | None
    quest_id: str | None
    region_id: str | None
    base_price: int | None
    markup_pct: float | None
    faction_modifier: float | None
    sell_back_ratio: float | None
    note: str | None
    created_at: str

    model_config = {"from_attributes": True}


class TransactionHistoryResponse(BaseModel):
    character_id: str
    total: int
    transactions: list[TransactionEntry]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{character_id}", response_model=WalletResponse)
async def get_wallet(character_id: str, token: Token, db: DB) -> WalletResponse:
    """Get the wallet balances for a character."""
    character = await load_character_owned(db, character_id, token.player_id)
    return WalletResponse(
        character_id=character.id,
        world_id=character.world_id,
        balances=character.wallet or {},
    )


@router.post("/grant", status_code=status.HTTP_200_OK, response_model=GrantResponse)
async def grant_currency(body: GrantRequest, token: Token, db: DB) -> GrantResponse:
    """Grant currency to a character (admin/quest reward operation).

    TODO(Phase 1): Add role-based access control.  Currently, the ownership
    check restricts grants to the character's own player.  DM (role="dm") and
    admin tokens should bypass ownership so they can grant rewards to other
    players' characters.  Until then, DM grants require the player to
    self-grant through their own session token.
    """
    character = await load_character_owned(db, body.character_id, token.player_id)

    new_balance = await credit(
        db,
        character,
        currency=body.currency,
        amount=body.amount,
        tx_type="grant",
        note=body.note or "Admin grant",
    )

    await db.commit()
    return GrantResponse(
        character_id=character.id,
        currency=body.currency,
        amount=body.amount,
        balance_after=new_balance,
    )


@router.get("/{character_id}/transactions", response_model=TransactionHistoryResponse)
async def get_transactions(
    character_id: str,
    token: Token,
    db: DB,
    limit: int = Query(default=50, ge=1, le=200, description="Max transactions per page"),
    offset: int = Query(default=0, ge=0, description="Transactions to skip"),
    tx_type: str | None = Query(default=None, description="Filter by transaction type"),
) -> TransactionHistoryResponse:
    """Get the transaction history for a character."""
    character = await load_character_owned(db, character_id, token.player_id)

    query = select(TransactionLog).where(TransactionLog.character_id == character.id)

    if tx_type is not None:
        query = query.where(TransactionLog.tx_type == tx_type)

    count_result = await db.execute(
        select(TransactionLog.id).where(TransactionLog.character_id == character.id)
        if tx_type is None
        else select(TransactionLog.id)
        .where(TransactionLog.character_id == character.id)
        .where(TransactionLog.tx_type == tx_type)
    )
    total = len(count_result.all())

    result = await db.execute(query.order_by(TransactionLog.created_at.desc()).offset(offset).limit(limit))
    rows = result.scalars().all()

    entries = [
        TransactionEntry(
            id=r.id,
            tx_type=r.tx_type,
            amount=r.amount,
            currency=r.currency,
            balance_after=r.balance_after,
            item_id=r.item_id,
            item_quantity=r.item_quantity,
            npc_id=r.npc_id,
            session_id=r.session_id,
            quest_id=r.quest_id,
            region_id=r.region_id,
            base_price=r.base_price,
            markup_pct=r.markup_pct,
            faction_modifier=r.faction_modifier,
            sell_back_ratio=r.sell_back_ratio,
            note=r.note,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]

    return TransactionHistoryResponse(
        character_id=character.id,
        total=total,
        transactions=entries,
    )
