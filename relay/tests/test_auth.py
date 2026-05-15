from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from relay.auth.middleware import (
    auth_middleware,
    require_role,
    require_tier,
)
from relay.auth.tokens import (
    AccountTokenPayload,
    SessionTokenPayload,
    create_account_token,
    create_session_token,
    decode_token,
)
from relay.config import settings
from relay.main import app
from relay.middleware.rate_limit import (
    _STALE_SECONDS,
    _buckets,
    _evict_stale,
    _get_rate_limit_key,
    clear_buckets,
)

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
        now = datetime.now(UTC)
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
        now = datetime.now(UTC)
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


# ---------------------------------------------------------------------------
# Tier enforcement
# ---------------------------------------------------------------------------


# Create a mini app to test tier/role dependencies in isolation.
_tier_app = FastAPI()
_tier_app.add_middleware(
    __import__("starlette.middleware.base", fromlist=["BaseHTTPMiddleware"]).BaseHTTPMiddleware,
    dispatch=auth_middleware,
)


@_tier_app.get("/tier2-only")
async def _tier2_route(
    token: Annotated[AccountTokenPayload | SessionTokenPayload, Depends(require_tier(2))],
) -> dict:
    return {"player_id": token.player_id}


@_tier_app.get("/dm-only")
async def _dm_route(
    token: Annotated[SessionTokenPayload, Depends(require_role("dm"))],
) -> dict:
    return {"role": token.role}


_tier_client = TestClient(_tier_app, raise_server_exceptions=False)


class TestTierEnforcement:
    def test_tier2_allows_tier2_user(self) -> None:
        token = create_account_token(player_id="p1", tier=2)
        resp = _tier_client.get("/tier2-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["player_id"] == "p1"

    def test_tier2_rejects_tier1_user(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        resp = _tier_client.get("/tier2-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "Tier 2" in resp.json()["detail"]["message"]

    def test_tier2_allows_higher_tier(self) -> None:
        token = create_account_token(player_id="p1", tier=3)
        resp = _tier_client.get("/tier2-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_tier2_without_token_returns_401(self) -> None:
        resp = _tier_client.get("/tier2-only")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_dm_allows_dm_role(self) -> None:
        token = create_session_token(
            player_id="p1",
            world_id="inkglass_dark",
            session_id="s1",
            tier=1,
            role="dm",
            mode="multiplayer",
        )
        resp = _tier_client.get("/dm-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "dm"

    def test_dm_rejects_player_role(self) -> None:
        token = create_session_token(
            player_id="p1",
            world_id="inkglass_dark",
            session_id="s1",
            tier=1,
            role="player",
            mode="solo",
        )
        resp = _tier_client.get("/dm-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "dm" in resp.json()["detail"]["message"]

    def test_dm_rejects_account_token(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        resp = _tier_client.get("/dm-only", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "session token" in resp.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Rate limiter — player-keyed and eviction
# ---------------------------------------------------------------------------


class TestRateLimiterKeying:
    def setup_method(self) -> None:
        clear_buckets()

    def test_authenticated_request_keyed_by_player_id(self) -> None:
        token = create_account_token(player_id="player_abc", tier=1)
        client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert "player:player_abc" in _buckets

    def test_unauthenticated_request_keyed_by_ip(self) -> None:
        # Health is exempt, so hit a protected endpoint without a token
        # to trigger rate limiter with IP-based key (middleware blocks, but
        # rate limiter runs after auth middleware so it won't reach rate_limit
        # for unauthenticated requests). Instead test _get_rate_limit_key directly.
        from unittest.mock import MagicMock

        req = MagicMock()
        req.state = MagicMock(spec=[])  # No 'token' attribute
        req.client = MagicMock()
        req.client.host = "192.168.1.100"
        assert _get_rate_limit_key(req) == "192.168.1.100"

    def test_authenticated_key_uses_player_id(self) -> None:
        from unittest.mock import MagicMock

        req = MagicMock()
        req.state.token = AccountTokenPayload(
            player_id="test_player",
            tier=1,
            iat=datetime.now(UTC),
            exp=datetime.now(UTC) + timedelta(hours=1),
        )
        assert _get_rate_limit_key(req) == "player:test_player"


class TestRateLimiterEviction:
    def setup_method(self) -> None:
        clear_buckets()

    def test_stale_buckets_are_evicted(self) -> None:
        from relay.middleware.rate_limit import _Bucket

        # Create a bucket and backdate its last_refill
        bucket = _Bucket()
        bucket.last_refill = time.monotonic() - _STALE_SECONDS - 10
        _buckets["stale_key"] = bucket

        # Create a fresh bucket
        fresh = _Bucket()
        _buckets["fresh_key"] = fresh

        _evict_stale()

        assert "stale_key" not in _buckets
        assert "fresh_key" in _buckets

    def test_fresh_buckets_not_evicted(self) -> None:
        from relay.middleware.rate_limit import _Bucket

        bucket = _Bucket()
        _buckets["recent_key"] = bucket

        _evict_stale()

        assert "recent_key" in _buckets

    def test_eviction_triggered_above_threshold(self) -> None:
        from relay.middleware.rate_limit import _EVICTION_THRESHOLD, _Bucket

        # Fill past threshold with stale buckets
        for i in range(_EVICTION_THRESHOLD + 5):
            b = _Bucket()
            b.last_refill = time.monotonic() - _STALE_SECONDS - 10
            _buckets[f"key_{i}"] = b

        # A request should trigger eviction
        token = create_account_token(player_id="trigger", tier=1)
        client.get("/me", headers={"Authorization": f"Bearer {token}"})

        # All stale buckets should be gone, only the new request's bucket remains
        assert len(_buckets) <= 2  # trigger + maybe one more
