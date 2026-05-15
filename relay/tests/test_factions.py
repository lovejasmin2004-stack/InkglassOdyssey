"""Tests for the faction reputation system.

Covers: tier resolution, standing changes, propagation to allies/rivals,
clamping, price modifier application, and endpoint integration.
"""

from __future__ import annotations

import pytest

from relay.auth.tokens import create_account_token, create_session_token
from relay.economy.pricing import (
    compute_buy_price,
    compute_sell_price,
    faction_tier,
)
from relay.factions.reputation import (
    apply_standing_change,
    clamp_standing,
    resolve_tier,
)

# ---------------------------------------------------------------------------
# Sample faction registry for propagation tests
# ---------------------------------------------------------------------------

FACTION_REGISTRY = {
    "merchant_guild": {
        "id": "merchant_guild",
        "name": "Merchant Guild",
        "allied_factions": ["artisan_collective"],
        "rival_factions": ["thieves_den"],
    },
    "artisan_collective": {
        "id": "artisan_collective",
        "name": "Artisan Collective",
        "allied_factions": ["merchant_guild"],
        "rival_factions": [],
    },
    "thieves_den": {
        "id": "thieves_den",
        "name": "Thieves' Den",
        "allied_factions": [],
        "rival_factions": ["merchant_guild"],
    },
}


# ---------------------------------------------------------------------------
# Unit tests — tier resolution
# ---------------------------------------------------------------------------


class TestFactionTier:
    def test_hostile_low_bound(self):
        assert faction_tier(-100) == "hostile"

    def test_hostile_upper_bound(self):
        assert faction_tier(-51) == "hostile"

    def test_unfriendly_low_bound(self):
        assert faction_tier(-50) == "unfriendly"

    def test_unfriendly_upper_bound(self):
        assert faction_tier(-1) == "unfriendly"

    def test_neutral(self):
        assert faction_tier(0) == "neutral"

    def test_friendly_low_bound(self):
        assert faction_tier(1) == "friendly"

    def test_friendly_upper_bound(self):
        assert faction_tier(50) == "friendly"

    def test_allied_low_bound(self):
        assert faction_tier(51) == "allied"

    def test_allied_upper_bound(self):
        assert faction_tier(100) == "allied"

    def test_clamps_above_100(self):
        assert faction_tier(150) == "allied"

    def test_clamps_below_minus_100(self):
        assert faction_tier(-200) == "hostile"


class TestResolveTierCustomThresholds:
    def test_custom_thresholds(self):
        thresholds = {"hostile": -30, "unfriendly": -29, "neutral": 0, "friendly": 1, "allied": 30}
        assert resolve_tier(31, thresholds) == "allied"
        assert resolve_tier(30, thresholds) == "allied"
        assert resolve_tier(29, thresholds) == "friendly"
        assert resolve_tier(0, thresholds) == "neutral"
        assert resolve_tier(-1, thresholds) == "unfriendly"
        assert resolve_tier(-29, thresholds) == "unfriendly"
        assert resolve_tier(-30, thresholds) == "hostile"

    def test_none_thresholds_uses_default(self):
        assert resolve_tier(51) == "allied"
        assert resolve_tier(0) == "neutral"
        assert resolve_tier(-51) == "hostile"


# ---------------------------------------------------------------------------
# Unit tests — clamping
# ---------------------------------------------------------------------------


class TestClampStanding:
    def test_within_range(self):
        assert clamp_standing(50) == 50

    def test_above_max(self):
        assert clamp_standing(150) == 100

    def test_below_min(self):
        assert clamp_standing(-200) == -100

    def test_at_bounds(self):
        assert clamp_standing(100) == 100
        assert clamp_standing(-100) == -100


# ---------------------------------------------------------------------------
# Unit tests — standing change + propagation
# ---------------------------------------------------------------------------


class TestApplyStandingChange:
    def test_simple_change(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10)
        assert standings["merchant_guild"] == 10
        assert changes["merchant_guild"]["old"] == 0
        assert changes["merchant_guild"]["new"] == 10
        assert changes["merchant_guild"]["tier"] == "friendly"

    def test_clamped_to_max(self):
        standings = {"merchant_guild": 95}
        apply_standing_change(standings, "merchant_guild", 20)
        assert standings["merchant_guild"] == 100

    def test_clamped_to_min(self):
        standings = {"merchant_guild": -95}
        apply_standing_change(standings, "merchant_guild", -20)
        assert standings["merchant_guild"] == -100

    def test_negative_delta(self):
        standings = {"merchant_guild": 50}
        changes = apply_standing_change(standings, "merchant_guild", -60)
        assert standings["merchant_guild"] == -10
        assert changes["merchant_guild"]["tier"] == "unfriendly"


class TestPropagation:
    def test_allied_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10, faction_registry=FACTION_REGISTRY)
        # Ally gets +50% = +5
        assert standings["artisan_collective"] == 5
        assert changes["artisan_collective"]["source"] == "allied_propagation"

    def test_rival_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10, faction_registry=FACTION_REGISTRY)
        # Rival gets -25% = floor(10 * -0.25) = floor(-2.5) = -3
        assert standings["thieves_den"] == -3
        assert changes["thieves_den"]["source"] == "rival_propagation"

    def test_negative_delta_propagation(self):
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", -20, faction_registry=FACTION_REGISTRY)
        # Direct: 0 + (-20) = -20
        assert standings["merchant_guild"] == -20
        # Ally: floor(-20 * 0.5) = -10
        assert standings["artisan_collective"] == -10
        # Rival: floor(-20 * -0.25) = floor(5) = 5
        assert standings["thieves_den"] == 5

    def test_propagation_clamps(self):
        standings = {"merchant_guild": 0, "artisan_collective": 98}
        apply_standing_change(standings, "merchant_guild", 20, faction_registry=FACTION_REGISTRY)
        # Ally gets +10, clamped: 98 + 10 = 100 (within bounds)
        assert standings["artisan_collective"] == 100

    def test_no_propagation_without_registry(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10)
        assert len(changes) == 1
        assert "artisan_collective" not in standings

    def test_unknown_faction_no_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "unknown_faction", 10, faction_registry=FACTION_REGISTRY)
        assert len(changes) == 1
        assert standings["unknown_faction"] == 10

    def test_small_delta_rounds_to_zero_no_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 1, faction_registry=FACTION_REGISTRY)
        # Ally: floor(1 * 0.5) = 0 → skipped
        # Rival: floor(1 * -0.25) = -1
        assert "artisan_collective" not in changes
        assert standings.get("artisan_collective", 0) == 0
        assert standings["thieves_den"] == -1


# ---------------------------------------------------------------------------
# Price modifier integration with pricing engine
# ---------------------------------------------------------------------------


class TestPricingWithFactionTiers:
    def test_allied_buy_price(self):
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=60)
        # allied → -20% → 100 * 0.80 = 80
        assert price == 80

    def test_friendly_buy_price(self):
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=25)
        # friendly → -10% → 100 * 0.90 = 90
        assert price == 90

    def test_neutral_buy_price(self):
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=0)
        assert price == 100

    def test_unfriendly_buy_price(self):
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=-25)
        # unfriendly → +25% → 100 * 1.25 = 125
        assert price == 125

    def test_allied_sell_price(self):
        price = compute_sell_price(base_value=100, faction_standing=60)
        # allied → sell_back 0.5 + 0.10 = 0.60 → 60
        assert price == 60

    def test_unfriendly_sell_price(self):
        price = compute_sell_price(base_value=100, faction_standing=-25)
        # unfriendly → sell_back 0.5 - 0.10 = 0.40 → 40
        assert price == 40

    def test_markup_with_faction_modifier(self):
        price = compute_buy_price(base_value=100, markup_pct=10.0, faction_standing=60)
        # 100 * 1.10 (markup) * 0.80 (allied) = 88
        assert price == 88


# ---------------------------------------------------------------------------
# Integration tests — endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_header():
    token = create_session_token(
        player_id="player_001",
        world_id="inkglass_dark",
        session_id="sess_001",
        tier=1,
        role="player",
        mode="solo",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def character_id(db_client, auth_header):
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Faction Tester",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 10,
                "dexterity": 14,
                "constitution": 12,
                "intelligence": 12,
                "wisdom": 14,
                "charisma": 10,
            },
            "skill_proficiencies": ["perception", "stealth"],
            "saving_throw_proficiencies": ["dexterity", "wisdom"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


class TestStandingEndpoint:
    def test_change_standing(self, db_client, session_header, character_id):
        resp = db_client.patch(
            "/factions/merchant_guild/standing",
            json={
                "character_id": character_id,
                "delta": 15,
                "reason": "quest_completion",
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["faction_id"] == "merchant_guild"

        direct = next(c for c in data["changes"] if c["faction_id"] == "merchant_guild")
        assert direct["old"] == 0
        assert direct["new"] == 15
        assert direct["tier"] == "friendly"

    def test_standing_with_propagation(self, db_client, session_header, character_id):
        resp = db_client.patch(
            "/factions/merchant_guild/standing",
            json={
                "character_id": character_id,
                "delta": 20,
                "reason": "major_quest",
            },
            headers=session_header,
        )
        assert resp.status_code == 200
        data = resp.json()

        changes_by_id = {c["faction_id"]: c for c in data["changes"]}

        direct = changes_by_id["merchant_guild"]
        assert direct["new"] == 20
        assert direct["source"] == "direct"

        ally = changes_by_id["artisan_collective"]
        assert ally["new"] == 10  # 50% of 20
        assert ally["source"] == "allied_propagation"

        rival = changes_by_id["thieves_den"]
        assert rival["new"] == -5  # floor(20 * -0.25) = -5
        assert rival["source"] == "rival_propagation"

    def test_standing_persists(self, db_client, session_header, character_id):
        db_client.patch(
            "/factions/merchant_guild/standing",
            json={
                "character_id": character_id,
                "delta": 30,
                "reason": "test",
            },
            headers=session_header,
        )

        # Read back via get standings
        resp = db_client.get(
            f"/factions/{character_id}/standings",
            headers=session_header,
        )
        assert resp.status_code == 200
        standings = resp.json()["standings"]
        mg = next(s for s in standings if s["faction_id"] == "merchant_guild")
        assert mg["standing"] == 30
        assert mg["tier"] == "friendly"

    def test_standing_accumulates(self, db_client, session_header, character_id):
        db_client.patch(
            "/factions/merchant_guild/standing",
            json={"character_id": character_id, "delta": 10, "reason": "first"},
            headers=session_header,
        )
        resp = db_client.patch(
            "/factions/merchant_guild/standing",
            json={"character_id": character_id, "delta": 15, "reason": "second"},
            headers=session_header,
        )
        assert resp.status_code == 200
        direct = next(c for c in resp.json()["changes"] if c["faction_id"] == "merchant_guild")
        assert direct["old"] == 10
        assert direct["new"] == 25

    def test_negative_standing(self, db_client, session_header, character_id):
        resp = db_client.patch(
            "/factions/merchant_guild/standing",
            json={"character_id": character_id, "delta": -60, "reason": "betrayal"},
            headers=session_header,
        )
        assert resp.status_code == 200
        direct = next(c for c in resp.json()["changes"] if c["faction_id"] == "merchant_guild")
        assert direct["new"] == -60
        assert direct["tier"] == "hostile"

    def test_character_not_found(self, db_client, session_header):
        resp = db_client.patch(
            "/factions/merchant_guild/standing",
            json={
                "character_id": "nonexistent",
                "delta": 10,
                "reason": "test",
            },
            headers=session_header,
        )
        assert resp.status_code == 404

    def test_propagation_affects_shop_prices(self, db_client, session_header, character_id):
        """Verify that a faction standing change alters the tier and thus shop pricing."""
        # Start neutral (standing 0)
        price_neutral = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=0)

        # Change standing to friendly (+25)
        db_client.patch(
            "/factions/merchant_guild/standing",
            json={"character_id": character_id, "delta": 25, "reason": "quest"},
            headers=session_header,
        )

        # Verify character's standing changed
        char = db_client.get(
            f"/character/{character_id}",
            headers=_make_auth_header(),
        ).json()
        standing = char["faction_standing"]["merchant_guild"]
        assert standing == 25

        # Compute new price with updated standing
        price_friendly = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=standing)
        assert price_neutral == 100
        assert price_friendly == 90  # friendly = -10%
        assert price_friendly < price_neutral


class TestGetStandingsEndpoint:
    def test_empty_standings(self, db_client, session_header, character_id):
        resp = db_client.get(
            f"/factions/{character_id}/standings",
            headers=session_header,
        )
        assert resp.status_code == 200
        assert resp.json()["standings"] == []

    def test_returns_all_standings(self, db_client, session_header, character_id):
        db_client.patch(
            "/factions/merchant_guild/standing",
            json={
                "character_id": character_id,
                "delta": 20,
                "reason": "test",
            },
            headers=session_header,
        )

        resp = db_client.get(
            f"/factions/{character_id}/standings",
            headers=session_header,
        )
        assert resp.status_code == 200
        standings = resp.json()["standings"]
        ids = {s["faction_id"] for s in standings}
        assert "merchant_guild" in ids
        assert "artisan_collective" in ids
        assert "thieves_den" in ids


def _make_auth_header() -> dict:
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Delta clamping (#5)
# ---------------------------------------------------------------------------


class TestDeltaClamping:
    def test_extreme_positive_delta_clamped(self):
        """Delta beyond +-200 is clamped to effective range."""
        standings: dict[str, int] = {"guild": -50}
        apply_standing_change(standings, "guild", 500)
        # 500 clamped to 200, -50 + 200 = 150, clamped to 100
        assert standings["guild"] == 100

    def test_extreme_negative_delta_clamped(self):
        standings: dict[str, int] = {"guild": 50}
        apply_standing_change(standings, "guild", -500)
        # -500 clamped to -200, 50 + (-200) = -150, clamped to -100
        assert standings["guild"] == -100

    def test_normal_delta_unchanged(self):
        """Deltas within range are not altered by clamping."""
        standings: dict[str, int] = {}
        apply_standing_change(standings, "guild", 15)
        assert standings["guild"] == 15


# ---------------------------------------------------------------------------
# Custom thresholds via resolve_tier wiring (#8)
# ---------------------------------------------------------------------------

FACTION_REGISTRY_CUSTOM_THRESHOLDS = {
    "strict_guild": {
        "id": "strict_guild",
        "name": "Strict Guild",
        "allied_factions": ["lenient_guild"],
        "rival_factions": [],
        "reputation_thresholds": {
            "hostile": -30,
            "unfriendly": -29,
            "neutral": 0,
            "friendly": 1,
            "allied": 70,
        },
    },
    "lenient_guild": {
        "id": "lenient_guild",
        "name": "Lenient Guild",
        "allied_factions": [],
        "rival_factions": [],
        "reputation_thresholds": {
            "hostile": -80,
            "unfriendly": -79,
            "neutral": 0,
            "friendly": 1,
            "allied": 20,
        },
    },
}


class TestCustomThresholdsPropagation:
    def test_direct_change_uses_custom_thresholds(self):
        """Standing 55 is 'allied' with defaults but 'friendly' with strict thresholds."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(
            standings, "strict_guild", 55, faction_registry=FACTION_REGISTRY_CUSTOM_THRESHOLDS
        )
        # Default thresholds: 55 >= 51 → allied
        # Strict thresholds: 55 < 70 → friendly
        assert changes["strict_guild"]["tier"] == "friendly"

    def test_propagated_ally_uses_own_thresholds(self):
        """Propagated ally uses its own custom thresholds, not the source faction's."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(
            standings, "strict_guild", 50, faction_registry=FACTION_REGISTRY_CUSTOM_THRESHOLDS
        )
        # Ally gets floor(50 * 0.5) = 25
        # Lenient thresholds: 25 >= 20 → allied
        assert changes["lenient_guild"]["tier"] == "allied"
        assert changes["lenient_guild"]["new"] == 25

    def test_no_thresholds_falls_back_to_default(self):
        """Factions without reputation_thresholds use default tier boundaries."""
        registry_no_thresholds = {
            "plain_guild": {
                "id": "plain_guild",
                "name": "Plain Guild",
                "allied_factions": [],
                "rival_factions": [],
            },
        }
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "plain_guild", 51, faction_registry=registry_no_thresholds)
        # Default thresholds: 51 → allied
        assert changes["plain_guild"]["tier"] == "allied"


# ---------------------------------------------------------------------------
# updated_at consistency (#4)
# ---------------------------------------------------------------------------


class TestStandingUpdatedAt:
    def test_standing_change_sets_updated_at(self, db_client, session_header, auth_header, character_id):
        """PATCH standing should update character.updated_at."""
        char_before = db_client.get(
            f"/character/{character_id}",
            headers=auth_header,
        ).json()
        original_updated = char_before.get("updated_at")

        db_client.patch(
            "/factions/merchant_guild/standing",
            json={"character_id": character_id, "delta": 10, "reason": "test"},
            headers=session_header,
        )

        char_after = db_client.get(
            f"/character/{character_id}",
            headers=auth_header,
        ).json()
        new_updated = char_after.get("updated_at")

        # updated_at should have changed (or been set if it was None)
        assert new_updated is not None
        assert new_updated != original_updated
