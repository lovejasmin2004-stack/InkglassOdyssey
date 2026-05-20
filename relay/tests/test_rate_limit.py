"""Rate-limit integration tests.

Verifies that rapid requests trigger 429 responses and that the error
body matches the ErrorResponse schema (code + message).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay.auth.tokens import create_account_token
from relay.main import app
from relay.middleware.rate_limit import _DEFAULT_RPM, _Bucket, _buckets, clear_buckets

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_buckets():
    clear_buckets()
    yield
    clear_buckets()


def _auth(player_id: str = "rate_test_player", tier: int = 1) -> dict[str, str]:
    token = create_account_token(player_id=player_id, tier=tier)
    return {"Authorization": f"Bearer {token}"}


class TestRateLimitEnforcement:
    def test_requests_under_limit_succeed(self) -> None:
        headers = _auth()
        for _ in range(5):
            resp = client.get("/me", headers=headers)
            assert resp.status_code == 200

    def test_rapid_requests_trigger_429(self) -> None:
        headers = _auth("burst_player")
        key = "player:burst_player"

        # Drain the bucket to 0 tokens
        bucket = _Bucket()
        bucket.tokens = 0.0
        _buckets[key] = bucket

        resp = client.get("/me", headers=headers)
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == "rate_limited"
        assert "message" in body

    def test_429_body_matches_error_schema(self) -> None:
        headers = _auth("schema_check_player")
        key = "player:schema_check_player"

        bucket = _Bucket()
        bucket.tokens = 0.0
        _buckets[key] = bucket

        resp = client.get("/me", headers=headers)
        assert resp.status_code == 429
        body = resp.json()
        assert set(body.keys()) <= {"code", "message", "turn_id", "narrative_hint"}
        assert isinstance(body["code"], str)
        assert isinstance(body["message"], str)

    def test_different_players_have_separate_buckets(self) -> None:
        headers_a = _auth("player_a")
        headers_b = _auth("player_b")

        # Drain player_a
        bucket = _Bucket()
        bucket.tokens = 0.0
        _buckets["player:player_a"] = bucket

        resp_a = client.get("/me", headers=headers_a)
        assert resp_a.status_code == 429

        resp_b = client.get("/me", headers=headers_b)
        assert resp_b.status_code == 200

    def test_health_exempt_from_rate_limit(self) -> None:
        # Even with bucket drained, health should pass
        bucket = _Bucket()
        bucket.tokens = 0.0
        _buckets["testclient"] = bucket

        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_bucket_refills_over_time(self) -> None:
        import time

        headers = _auth("refill_player")
        key = "player:refill_player"

        bucket = _Bucket()
        bucket.tokens = 0.0
        bucket.last_refill = time.monotonic() - 2.0  # 2 seconds ago
        _buckets[key] = bucket

        # After 2 seconds, bucket should have refilled ~2 tokens (60 RPM = 1/sec)
        resp = client.get("/me", headers=headers)
        assert resp.status_code == 200

    def test_exhausting_full_bucket(self) -> None:
        headers = _auth("exhaust_player")

        successes = 0
        for _i in range(_DEFAULT_RPM + 5):
            resp = client.get("/me", headers=headers)
            if resp.status_code == 200:
                successes += 1
            elif resp.status_code == 429:
                break

        assert successes <= _DEFAULT_RPM
        assert successes > 0
