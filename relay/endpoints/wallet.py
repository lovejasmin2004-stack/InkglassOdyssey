"""Wallet endpoints — balance query and admin grant.

Invariant #14: all economy transactions through relay endpoints.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import get_current_token
from relay.auth.tokens import AccountTokenPayload, SessionTokenPayload
from relay.database import get_db
from relay.economy.wallet import credit
from relay.models import Character, TransactionLog

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
    note: str | None
    created_at: str

    model_config = {"from_attributes": True}


class TransactionHistoryResponse(BaseModel):
    character_id: str
    transactions: list[TransactionEntry]


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{character_id}", response_model=WalletResponse)
async def get_wallet(character_id: str, token: Token, db: DB) -> WalletResponse:
    """Get the wallet balances for a character."""
    character = await _get_character(db, character_id, token.player_id)
    return WalletResponse(
        character_id=character.id,
        world_id=character.world_id,
        balances=character.wallet or {},
    )


@router.post("/grant", status_code=status.HTTP_200_OK, response_model=GrantResponse)
async def grant_currency(body: GrantRequest, token: Token, db: DB) -> GrantResponse:
    """Grant currency to a character (admin/quest reward operation)."""
    character = await _get_character(db, body.character_id, token.player_id)

    new_balance = await credit(
        db, character,
        currency=body.currency,
        amount=body.amount,
        tx_type="grant",
        note=body.note or "Admin grant",
    )

    await db.flush()
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
    limit: int = 50,
) -> TransactionHistoryResponse:
    """Get the transaction history for a character."""
    character = await _get_character(db, character_id, token.player_id)

    result = await db.execute(
        select(TransactionLog)
        .where(TransactionLog.character_id == character.id)
        .order_by(TransactionLog.created_at.desc())
        .limit(min(limit, 200))
    )
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
            note=r.note,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]

    return TransactionHistoryResponse(
        character_id=character.id,
        transactions=entries,
    )
