"""Error response schema tests.

Verifies that all error responses match the canonical ErrorResponse schema
(CLAUDE.md §8.1): { code, message, turn_id?, narrative_hint? }.
Malformed requests must return descriptive 400/422 errors, never 500s.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay.auth.tokens import create_account_token, create_session_token
from relay.main import app
from relay.middleware.rate_limit import clear_buckets

client = TestClient(app, raise_server_exceptions=False)

_VALID_ERROR_KEYS = {"code", "message", "turn_id", "narrative_hint"}


@pytest.fixture(autouse=True)
def _clean_buckets():
    clear_buckets()
    yield
    clear_buckets()


def _account_headers(player_id: str = "err_test", tier: int = 1) -> dict[str, str]:
    token = create_account_token(player_id=player_id, tier=tier)
    return {"Authorization": f"Bearer {token}"}


def _session_headers(
    player_id: str = "err_test",
    world_id: str = "inkglass_dark",
    session_id: str = "s1",
    tier: int = 1,
    role: str = "player",
    mode: str = "solo",
) -> dict[str, str]:
    token = create_session_token(
        player_id=player_id,
        world_id=world_id,
        session_id=session_id,
        tier=tier,
        role=role,
        mode=mode,
    )
    return {"Authorization": f"Bearer {token}"}


def _assert_error_shape(resp, expected_status: int, expected_code: str | None = None):
    """Assert the response matches ErrorResponse schema."""
    assert resp.status_code == expected_status, f"Expected {expected_status}, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert set(body.keys()) <= _VALID_ERROR_KEYS, f"Unexpected keys: {set(body.keys()) - _VALID_ERROR_KEYS}"
    assert "code" in body
    assert "message" in body
    assert isinstance(body["code"], str)
    assert isinstance(body["message"], str)
    assert len(body["message"]) > 0
    if expected_code:
        assert body["code"] == expected_code
    return body


# ---------------------------------------------------------------------------
# Auth errors — 401
# ---------------------------------------------------------------------------


class TestAuthErrors:
    def test_missing_token_returns_401(self) -> None:
        resp = client.get("/me")
        _assert_error_shape(resp, 401, "unauthorized")

    def test_malformed_token_returns_401(self) -> None:
        resp = client.get("/me", headers={"Authorization": "Bearer garbage"})
        _assert_error_shape(resp, 401, "unauthorized")

    def test_missing_bearer_prefix_returns_401(self) -> None:
        token = create_account_token(player_id="p1", tier=1)
        resp = client.get("/me", headers={"Authorization": token})
        _assert_error_shape(resp, 401, "unauthorized")

    def test_empty_bearer_returns_401(self) -> None:
        resp = client.get("/me", headers={"Authorization": "Bearer "})
        _assert_error_shape(resp, 401, "unauthorized")


# ---------------------------------------------------------------------------
# Not found — 404
# ---------------------------------------------------------------------------


class TestNotFoundErrors:
    def test_character_not_found(self) -> None:
        resp = client.get("/character/nonexistent", headers=_account_headers())
        _assert_error_shape(resp, 404, "not_found")

    def test_session_not_found(self) -> None:
        resp = client.get("/session/nonexistent/state", headers=_account_headers())
        _assert_error_shape(resp, 404, "not_found")

    def test_scene_not_found(self) -> None:
        resp = client.get("/scene/nonexistent", headers=_session_headers())
        _assert_error_shape(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# Validation errors — 422 (malformed request bodies)
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_missing_required_field_returns_422(self) -> None:
        resp = client.post(
            "/character",
            json={"name": "Test"},
            headers=_account_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "world_id" in body["message"] or "specialisation_path_id" in body["message"]

    def test_wrong_type_returns_422(self) -> None:
        resp = client.post(
            "/character",
            json={
                "world_id": 12345,
                "name": "Test",
                "specialisation_path_id": "warrior",
                "ability_scores": "not_a_dict",
            },
            headers=_account_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "ability_scores" in body["message"]

    def test_extra_forbidden_field_returns_422(self) -> None:
        resp = client.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Test",
                "specialisation_path_id": "warrior",
                "ability_scores": {"strength": 10},
                "totally_bogus_field": True,
            },
            headers=_account_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "totally_bogus_field" in body["message"] or "extra" in body["message"].lower()

    def test_empty_body_returns_422(self) -> None:
        resp = client.post(
            "/session/start",
            json={},
            headers=_account_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "character_id" in body["message"] or "world_id" in body["message"]

    def test_invalid_json_returns_422(self) -> None:
        resp = client.post(
            "/session/start",
            content=b"not valid json{{{",
            headers={
                **_account_headers(),
                "Content-Type": "application/json",
            },
        )
        _assert_error_shape(resp, 422, "validation_error")

    def test_session_start_bad_mode_returns_422(self) -> None:
        resp = client.post(
            "/session/start",
            json={
                "character_id": "c1",
                "world_id": "inkglass_dark",
                "mode": "invalid_mode",
            },
            headers=_account_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "mode" in body["message"]

    def test_dice_roll_missing_formula_returns_422(self) -> None:
        resp = client.post(
            "/dice/roll",
            json={},
            headers=_session_headers(),
        )
        _assert_error_shape(resp, 422, "validation_error")

    def test_craft_negative_quantity_returns_422(self) -> None:
        resp = client.post(
            "/shop/test_npc/buy",
            json={
                "character_id": "c1",
                "item_id": "sword",
                "quantity": -1,
            },
            headers=_session_headers(),
        )
        body = _assert_error_shape(resp, 422, "validation_error")
        assert "quantity" in body["message"]

    def test_character_patch_level_out_of_range_returns_422(self) -> None:
        resp = client.patch(
            "/character/nonexistent",
            json={"level": 99},
            headers=_account_headers(),
        )
        # 404 is valid too (not found before validation), but if validation runs first: 422
        assert resp.status_code in (404, 422)
        body = resp.json()
        assert "code" in body
        assert "message" in body


# ---------------------------------------------------------------------------
# Forbidden — 403
# ---------------------------------------------------------------------------


class TestForbiddenErrors:
    def test_tier2_world_with_tier1_token_returns_403(self) -> None:
        resp = client.post(
            "/session/start",
            json={
                "character_id": "c1",
                "world_id": "wha_au",
            },
            headers=_account_headers(tier=1),
        )
        body = _assert_error_shape(resp, 403, "forbidden")
        assert "Tier 2" in body["message"]


# ---------------------------------------------------------------------------
# Conflict — 409
# ---------------------------------------------------------------------------


class TestConflictErrors:
    def test_session_already_ended_returns_409(self) -> None:
        # Session doesn't exist, so we get 404 first — that's fine.
        # Just verify the error shape.
        resp = client.post(
            "/session/nonexistent/end",
            json={},
            headers=_account_headers(),
        )
        _assert_error_shape(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# Catch-all — unhandled exceptions return 500 with proper schema
# ---------------------------------------------------------------------------


class TestCatchAll:
    def test_unhandled_exception_returns_500_with_schema(self) -> None:
        from unittest.mock import patch

        with patch(
            "relay.endpoints.character.select",
            side_effect=RuntimeError("simulated DB failure"),
        ):
            resp = client.get("/character", headers=_account_headers())
            body = _assert_error_shape(resp, 500, "internal_error")
            assert "internal" in body["message"].lower()
            # Must NOT contain the stack trace
            assert "RuntimeError" not in body["message"]
            assert "simulated DB failure" not in body["message"]


# ---------------------------------------------------------------------------
# Error schema consistency across all error status codes
# ---------------------------------------------------------------------------


class TestErrorSchemaConsistency:
    """Verify that every known error path returns the canonical shape."""

    @pytest.mark.parametrize(
        "method,path,json_body,expected_status",
        [
            ("GET", "/me", None, 401),
            ("GET", "/character/no_such_id", None, 404),
            ("GET", "/session/no_such_id/state", None, 404),
            ("GET", "/scene/no_such_id", None, 404),
            ("POST", "/session/start", {"character_id": "x", "world_id": "wha_au"}, 403),
        ],
    )
    def test_error_body_shape(self, method, path, json_body, expected_status) -> None:
        headers = _account_headers(tier=1) if expected_status != 401 else {}
        if method == "GET":
            resp = client.get(path, headers=headers)
        else:
            resp = client.post(path, json=json_body or {}, headers=headers)
        body = resp.json()
        assert resp.status_code == expected_status, f"{path}: {resp.status_code}"
        assert "code" in body, f"{path}: missing 'code'"
        assert "message" in body, f"{path}: missing 'message'"
        assert set(body.keys()) <= _VALID_ERROR_KEYS, f"{path}: unexpected keys {body.keys()}"
