from __future__ import annotations

from fastapi.testclient import TestClient

import relay.config as _config
from relay.auth.tokens import create_account_token, create_session_token

# Reusable payload matching Andalu's world and specialisation
_CREATE_PAYLOAD = {
    "world_id": "inkglass_dark",
    "name": "Andalu",
    "specialisation_path_id": "scout",
    "ability_scores": {
        "strength": 10,
        "dexterity": 16,
        "constitution": 14,
        "intelligence": 12,
        "wisdom": 14,
        "charisma": 10,
    },
    "skill_proficiencies": ["stealth", "perception", "survival"],
    "saving_throw_proficiencies": ["strength", "dexterity"],
}


def _auth(player_id: str = "player_001") -> dict[str, str]:
    token = create_account_token(player_id=player_id, tier=1)
    return {"Authorization": f"Bearer {token}"}


def _dm_auth(player_id: str = "player_001") -> dict[str, str]:
    """Session token with DM role — can write protected fields."""
    token = create_session_token(
        player_id=player_id,
        world_id="inkglass_dark",
        session_id="s1",
        tier=1,
        role="dm",
        mode="multiplayer",
    )
    return {"Authorization": f"Bearer {token}"}


def _tier2_auth(player_id: str = "player_001") -> dict[str, str]:
    token = create_account_token(player_id=player_id, tier=2)
    return {"Authorization": f"Bearer {token}"}


class TestCreateCharacter:
    def test_post_creates_character(self, db_client: TestClient) -> None:
        r = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth())
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "Andalu"
        assert data["world_id"] == "inkglass_dark"
        assert data["level"] == 1
        assert data["player_id"] == "player_001"

    def test_post_computes_hp_and_ac(self, db_client: TestClient) -> None:
        r = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth())
        assert r.status_code == 201
        data = r.json()
        # CON 14 -> mod +2, default d8 hit die -> hp_max = 8 + 2 = 10
        assert data["hp_max"] == 10
        assert data["hp_current"] == data["hp_max"]
        # DEX 16 -> mod +3, unarmoured AC = 10 + 3 = 13
        assert data["ac"] == 13

    def test_post_returns_id(self, db_client: TestClient) -> None:
        r = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth())
        assert r.status_code == 201
        assert r.json()["id"] is not None

    def test_post_without_token_returns_401(self, db_client: TestClient) -> None:
        r = db_client.post("/character", json=_CREATE_PAYLOAD)
        assert r.status_code == 401

    def test_post_sets_wallet_to_zero(self, db_client: TestClient) -> None:
        r = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth())
        assert r.json()["wallet"] == {"gold": 0}

    def test_post_tier2_world_requires_tier2(self, db_client: TestClient) -> None:
        payload = {**_CREATE_PAYLOAD, "world_id": "wha_au"}
        r = db_client.post("/character", json=payload, headers=_auth())
        assert r.status_code == 403
        assert "Tier 2" in r.json()["message"]

    def test_post_tier2_world_allowed_for_tier2_player(self, db_client: TestClient) -> None:
        payload = {**_CREATE_PAYLOAD, "world_id": "wha_au"}
        r = db_client.post("/character", json=payload, headers=_tier2_auth())
        assert r.status_code == 201
        assert r.json()["world_id"] == "wha_au"


class TestListCharacters:
    def test_list_empty(self, db_client: TestClient) -> None:
        r = db_client.get("/character", headers=_auth())
        assert r.status_code == 200
        assert r.json() == []

    def test_list_returns_own_characters(self, db_client: TestClient) -> None:
        db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth("player_001"))
        db_client.post(
            "/character",
            json={**_CREATE_PAYLOAD, "name": "Second"},
            headers=_auth("player_001"),
        )
        # Another player's character
        db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth("player_002"))

        r = db_client.get("/character", headers=_auth("player_001"))
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_list_filter_by_world_id(self, db_client: TestClient) -> None:
        db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth())
        db_client.post(
            "/character",
            json={**_CREATE_PAYLOAD, "world_id": "murim", "name": "Wuxia Char"},
            headers=_auth(),
        )

        r = db_client.get("/character?world_id=murim", headers=_auth())
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["world_id"] == "murim"

    def test_list_without_token_returns_401(self, db_client: TestClient) -> None:
        r = db_client.get("/character")
        assert r.status_code == 401


class TestGetCharacter:
    def test_get_returns_character(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        r = db_client.get(f"/character/{char_id}", headers=_auth())
        assert r.status_code == 200
        assert r.json()["id"] == char_id
        assert r.json()["name"] == "Andalu"

    def test_get_nonexistent_returns_404(self, db_client: TestClient) -> None:
        r = db_client.get("/character/does-not-exist", headers=_auth())
        assert r.status_code == 404
        assert r.json()["code"] == "not_found"

    def test_get_other_players_character_returns_403(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth("player_001")).json()
        char_id = created["id"]

        r = db_client.get(f"/character/{char_id}", headers=_auth("player_002"))
        assert r.status_code == 403

    def test_get_without_token_returns_401(self, db_client: TestClient) -> None:
        r = db_client.get("/character/any-id")
        assert r.status_code == 401


class TestPatchCharacter:
    def test_patch_updates_name(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        r = db_client.patch(f"/character/{char_id}", json={"name": "Andalu Renamed"}, headers=_auth())
        assert r.status_code == 200
        assert r.json()["name"] == "Andalu Renamed"

    def test_patch_updates_rp_voice_notes(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        r = db_client.patch(
            f"/character/{char_id}",
            json={"rp_voice_notes": "Speaks softly, deliberate pauses"},
            headers=_auth(),
        )
        assert r.status_code == 200
        assert r.json()["rp_voice_notes"] == "Speaks softly, deliberate pauses"

    def test_patch_protected_field_rejected_for_player(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        # Temporarily disable admin_mode so protection kicks in
        _config.settings.admin_mode = False
        try:
            r = db_client.patch(
                f"/character/{char_id}",
                json={"wallet": {"inkglass_dark": 9999}},
                headers=_auth(),
            )
            assert r.status_code == 403
            assert "protected fields" in r.json()["message"]
            assert "wallet" in r.json()["message"]
        finally:
            _config.settings.admin_mode = True

    def test_patch_protected_field_allowed_for_dm(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_dm_auth()).json()
        char_id = created["id"]

        # Disable admin_mode to prove DM role alone bypasses protection
        _config.settings.admin_mode = False
        try:
            r = db_client.patch(
                f"/character/{char_id}",
                json={"wallet": {"inkglass_dark": 450}},
                headers=_dm_auth(),
            )
            assert r.status_code == 200
            assert r.json()["wallet"]["inkglass_dark"] == 450
        finally:
            _config.settings.admin_mode = True

    def test_patch_faction_standing_via_dm(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_dm_auth()).json()
        char_id = created["id"]

        r = db_client.patch(
            f"/character/{char_id}",
            json={"faction_standing": {"witches_circle": 20, "knights_moralis": -15}},
            headers=_dm_auth(),
        )
        assert r.status_code == 200
        assert r.json()["faction_standing"]["witches_circle"] == 20

    def test_patch_preserves_unpatched_fields(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        db_client.patch(f"/character/{char_id}", json={"name": "New Name"}, headers=_auth())
        r = db_client.get(f"/character/{char_id}", headers=_auth())
        # World and level must be untouched
        assert r.json()["world_id"] == "inkglass_dark"
        assert r.json()["level"] == 1

    def test_patch_updates_updated_at(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]
        original_updated_at = created["updated_at"]

        import time

        time.sleep(0.01)
        db_client.patch(f"/character/{char_id}", json={"name": "X"}, headers=_auth())
        r = db_client.get(f"/character/{char_id}", headers=_auth())
        assert r.json()["updated_at"] >= original_updated_at

    def test_patch_nonexistent_returns_404(self, db_client: TestClient) -> None:
        r = db_client.patch("/character/does-not-exist", json={"name": "X"}, headers=_auth())
        assert r.status_code == 404

    def test_patch_other_players_character_returns_403(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth("player_001")).json()
        char_id = created["id"]

        r = db_client.patch(f"/character/{char_id}", json={"name": "Stolen"}, headers=_auth("player_002"))
        assert r.status_code == 403

    def test_patch_multiple_protected_fields_listed_in_error(self, db_client: TestClient) -> None:
        created = db_client.post("/character", json=_CREATE_PAYLOAD, headers=_auth()).json()
        char_id = created["id"]

        _config.settings.admin_mode = False
        try:
            r = db_client.patch(
                f"/character/{char_id}",
                json={"wallet": {"inkglass_dark": 100}, "hp_current": 99},
                headers=_auth(),
            )
            assert r.status_code == 403
            assert "hp_current" in r.json()["message"]
            assert "wallet" in r.json()["message"]
        finally:
            _config.settings.admin_mode = True
