from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from relay.auth.tokens import (
    AccountTokenPayload,
    SessionTokenPayload,
    create_account_token,
    create_session_token,
    decode_token,
)
from relay.config import settings
from relay.main import app

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Token creation and decoding
# ---------------------------------------------------------------------------

class TestTokenCreation:
    def test_create_account_token_decodes(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        payload = decode_token(token)
        assert isinstance(payload, AccountTokenPayload)
        assert payload.player_id == "p1"
        assert payload.tier == 1
        assert payload.token_type == "account"

    def test_create_session_token_decodes(self) -> None:
        token = create_session_token(
            player_id="p1",
            world_id="inkglass_dark",
            session_id="sess_001",
            tier=1,
            role="player",
            mode="solo",
        )
        payload = decode_token(token)
        assert isinstance(payload, SessionTokenPayload)
        assert payload.world_id == "inkglass_dark"
        assert payload.role == "player"
        assert payload.mode == "solo"
        assert payload.token_type == "session"

    def test_expired_token_raises(self) -> None:
        now = datetime.now(timezone.utc)
        raw = {
            "player_id": "p1",
            "tier": 1,
            "token_type": "account",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        }
        expired = jwt.encode(raw, settings.jwt_secret, algorithm="HS256")
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_token(expired)

    def test_tampered_token_raises(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        with pytest.raises(jwt.PyJWTError):
            decode_token(token + "tampered")


# ---------------------------------------------------------------------------
# HTTP endpoint auth
# ---------------------------------------------------------------------------

class TestEndpointAuth:
    def test_health_requires_no_token(self) -> None:
        response = client.get("/health")
        assert response.status_code == 200

    def test_me_with_valid_account_token_passes(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.json()
        assert data["player_id"] == "p1"
        assert data["token_type"] == "account"

    def test_me_with_valid_session_token_passes(self) -> None:
        token = create_session_token(
            player_id="p1",
            world_id="inkglass_dark",
            session_id="sess_001",
            tier=1,
        )
        response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.json()
        assert data["world_id"] == "inkglass_dark"

    def test_me_without_token_returns_401(self) -> None:
        response = client.get("/me")
        assert response.status_code == 401

    def test_me_with_bad_token_returns_401(self) -> None:
        response = client.get("/me", headers={"Authorization": "Bearer not.a.real.token"})
        assert response.status_code == 401

    def test_me_with_expired_token_returns_401(self) -> None:
        now = datetime.now(timezone.utc)
        raw = {
            "player_id": "p1",
            "tier": 1,
            "token_type": "account",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        }
        expired = jwt.encode(raw, settings.jwt_secret, algorithm="HS256")
        response = client.get("/me", headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401

    def test_protected_route_via_middleware_without_token_returns_401(self) -> None:
        # Hits the middleware path (no Depends, raw path check)
        response = client.get("/me")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "unauthorized"
