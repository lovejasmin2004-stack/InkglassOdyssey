"""Wallet operations — credit, debit, and balance queries.

The wallet is stored on the Character model as ``wallet: dict[str, int]``
keyed by currency ID (typically the world_id, e.g. ``inkglass_dark``).
All mutations go through this module to enforce Invariant #14
(all economy transactions through relay endpoints).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from relay.models import Character, TransactionLog

logger = logging.getLogger(__name__)


class InsufficientFunds(Exception):
    """Raised when a debit would take the balance below zero."""

    def __init__(self, currency: str, balance: int, amount: int) -> None:
        self.currency = currency
        self.balance = balance
        self.amount = amount
        super().__init__(
            f"Insufficient {currency}: have {balance}, need {amount}"
        )


async def get_balance(character: Character, currency: str) -> int:
    """Return the balance for a given currency, defaulting to 0."""
    wallet: dict[str, int] = character.wallet or {}
    return wallet.get(currency, 0)


async def credit(
    db: AsyncSession,
    character: Character,
    *,
    currency: str,
    amount: int,
    tx_type: str,
    item_id: str | None = None,
    item_quantity: int | None = None,
    npc_id: str | None = None,
    session_id: str | None = None,
    base_price: int | None = None,
    markup_pct: float | None = None,
    faction_modifier: float | None = None,
    sell_back_ratio: float | None = None,
    note: str | None = None,
) -> int:
    """Add funds to a character's wallet and log the transaction.

    Returns the new balance.
    """
    if amount < 0:
        raise ValueError("credit amount must be non-negative")

    wallet = dict(character.wallet or {})
    old_balance = wallet.get(currency, 0)
    new_balance = old_balance + amount
    wallet[currency] = new_balance
    character.wallet = wallet
    character.updated_at = datetime.now(timezone.utc)

    tx = TransactionLog(
        id=f"tx_{uuid.uuid4().hex[:12]}",
        player_id=character.player_id,
        character_id=character.id,
        world_id=character.world_id,
        tx_type=tx_type,
        amount=amount,
        currency=currency,
        balance_after=new_balance,
        item_id=item_id,
        item_quantity=item_quantity,
        npc_id=npc_id,
        session_id=session_id,
        base_price=base_price,
        markup_pct=markup_pct,
        faction_modifier=faction_modifier,
        sell_back_ratio=sell_back_ratio,
        note=note,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tx)

    logger.info(
        "Wallet credited",
        extra={
            "character_id": character.id,
            "currency": currency,
            "amount": amount,
            "balance_after": new_balance,
            "tx_type": tx_type,
        },
    )
    return new_balance


async def debit(
    db: AsyncSession,
    character: Character,
    *,
    currency: str,
    amount: int,
    tx_type: str,
    item_id: str | None = None,
    item_quantity: int | None = None,
    npc_id: str | None = None,
    session_id: str | None = None,
    base_price: int | None = None,
    markup_pct: float | None = None,
    faction_modifier: float | None = None,
    sell_back_ratio: float | None = None,
    note: str | None = None,
) -> int:
    """Remove funds from a character's wallet and log the transaction.

    Raises InsufficientFunds if the balance would go negative.
    Returns the new balance.
    """
    if amount < 0:
        raise ValueError("debit amount must be non-negative")

    wallet = dict(character.wallet or {})
    old_balance = wallet.get(currency, 0)
    if old_balance < amount:
        raise InsufficientFunds(currency, old_balance, amount)

    new_balance = old_balance - amount
    wallet[currency] = new_balance
    character.wallet = wallet
    character.updated_at = datetime.now(timezone.utc)

    tx = TransactionLog(
        id=f"tx_{uuid.uuid4().hex[:12]}",
        player_id=character.player_id,
        character_id=character.id,
        world_id=character.world_id,
        tx_type=tx_type,
        amount=-amount,
        currency=currency,
        balance_after=new_balance,
        item_id=item_id,
        item_quantity=item_quantity,
        npc_id=npc_id,
        session_id=session_id,
        base_price=base_price,
        markup_pct=markup_pct,
        faction_modifier=faction_modifier,
        sell_back_ratio=sell_back_ratio,
        note=note,
        created_at=datetime.now(timezone.utc),
    )
    db.add(tx)

    logger.info(
        "Wallet debited",
        extra={
            "character_id": character.id,
            "currency": currency,
            "amount": amount,
            "balance_after": new_balance,
            "tx_type": tx_type,
        },
    )
    return new_balance
