from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pydantic import BaseModel

from relay.config import settings

_ALGORITHM = "HS256"
_ACCOUNT_TOKEN_TTL_DAYS = 30
_SESSION_TOKEN_TTL_HOURS = 12


class AccountTokenPayload(BaseModel):
    player_id: str
    tier: int
    token_type: Literal["account"] = "account"
    iat: datetime
    exp: datetime


class SessionTokenPayload(BaseModel):
    player_id: str
    world_id: str
    session_id: str
    tier: int
    role: Literal["player", "dm"]
    mode: Literal["solo", "multiplayer"]
    token_type: Literal["session"] = "session"
    iat: datetime
    exp: datetime


def create_account_token(player_id: str, tier: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "player_id": player_id,
        "tier": tier,
        "token_type": "account",
        "iat": now,
        "exp": now + timedelta(days=_ACCOUNT_TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def create_session_token(
    *,
    player_id: str,
    world_id: str,
    session_id: str,
    tier: int,
    role: Literal["player", "dm"] = "player",
    mode: Literal["solo", "multiplayer"] = "solo",
) -> str:
    now = datetime.now(UTC)
    payload = {
        "player_id": player_id,
        "world_id": world_id,
        "session_id": session_id,
        "tier": tier,
        "role": role,
        "mode": mode,
        "token_type": "session",
        "iat": now,
        "exp": now + timedelta(hours=_SESSION_TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_token(token: str) -> AccountTokenPayload | SessionTokenPayload:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    raw = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    if raw.get("token_type") == "session":
        return SessionTokenPayload.model_validate(raw)
    return AccountTokenPayload.model_validate(raw)
