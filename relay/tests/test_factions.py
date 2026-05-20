"""Tests for the faction reputation system.

Covers: tier resolution, standing changes, propagation to allies/rivals,
clamping, price modifier application, propagation cap, phantom prevention,
reason tracking, tier transitions, overlap validation, and endpoint integration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from relay.auth.tokens import create_account_token
from relay.economy.pricing import (
    compute_buy_price,
    compute_sell_price,
    faction_tier,
)
from relay.factions.reputation import (
    _MAX_PROPAGATED_DELTA,
    DEFAULT_THRESHOLDS,
    apply_standing_change,
    clamp_standing,
    resolve_tier,
)
from relay.schemas import Faction

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
    """Tier boundaries (DEFAULT_THRESHOLDS):
    hostile: ≤-51, unfriendly: -50..-11, neutral: -10..10,
    friendly: 11..50, allied: ≥51.
    """

    def test_hostile_low_bound(self):
        assert faction_tier(-100) == "hostile"

    def test_hostile_upper_bound(self):
        assert faction_tier(-51) == "hostile"

    def test_unfriendly_low_bound(self):
        assert faction_tier(-50) == "unfriendly"

    def test_unfriendly_upper_bound(self):
        assert faction_tier(-11) == "unfriendly"

    def test_neutral_low_bound(self):
        assert faction_tier(-10) == "neutral"

    def test_neutral_zero(self):
        assert faction_tier(0) == "neutral"

    def test_neutral_upper_bound(self):
        assert faction_tier(10) == "neutral"

    def test_friendly_low_bound(self):
        assert faction_tier(11) == "friendly"

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

    def test_default_thresholds_constant_used(self):
        """Verify faction_tier delegates to resolve_tier with DEFAULT_THRESHOLDS."""
        for standing in range(-100, 101):
            assert faction_tier(standing) == resolve_tier(standing)


class TestResolveTierCustomThresholds:
    def test_custom_thresholds(self):
        thresholds = {"hostile": -30, "unfriendly": -29, "neutral": -5, "friendly": 6, "allied": 30}
        assert resolve_tier(31, thresholds) == "allied"
        assert resolve_tier(30, thresholds) == "allied"
        assert resolve_tier(29, thresholds) == "friendly"
        assert resolve_tier(6, thresholds) == "friendly"
        assert resolve_tier(5, thresholds) == "neutral"
        assert resolve_tier(-5, thresholds) == "neutral"
        assert resolve_tier(-6, thresholds) == "unfriendly"
        assert resolve_tier(-29, thresholds) == "unfriendly"
        assert resolve_tier(-30, thresholds) == "hostile"

    def test_none_thresholds_uses_default(self):
        assert resolve_tier(51) == "allied"
        assert resolve_tier(0) == "neutral"
        assert resolve_tier(-51) == "hostile"

    def test_default_thresholds_accessible(self):
        """DEFAULT_THRESHOLDS is importable and has all required keys."""
        assert set(DEFAULT_THRESHOLDS.keys()) == {"hostile", "unfriendly", "neutral", "friendly", "allied"}
        # Ordering invariant: hostile < unfriendly <= neutral <= friendly < allied
        assert DEFAULT_THRESHOLDS["hostile"] < DEFAULT_THRESHOLDS["unfriendly"]
        assert DEFAULT_THRESHOLDS["unfriendly"] <= DEFAULT_THRESHOLDS["neutral"]
        assert DEFAULT_THRESHOLDS["neutral"] <= DEFAULT_THRESHOLDS["friendly"]
        assert DEFAULT_THRESHOLDS["friendly"] < DEFAULT_THRESHOLDS["allied"]


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
        assert changes["merchant_guild"]["tier"] == "neutral"

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
        assert changes["merchant_guild"]["tier"] == "neutral"

    def test_change_report_includes_tier_transition(self):
        """Change report includes old_tier, tier_changed fields."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 15)
        assert changes["merchant_guild"]["old_tier"] == "neutral"
        assert changes["merchant_guild"]["tier"] == "friendly"
        assert changes["merchant_guild"]["tier_changed"] is True

    def test_no_tier_change(self):
        """tier_changed is False when standing changes within the same tier."""
        standings = {"merchant_guild": 5}
        changes = apply_standing_change(standings, "merchant_guild", 5)
        assert changes["merchant_guild"]["old_tier"] == "neutral"
        assert changes["merchant_guild"]["tier"] == "neutral"
        assert changes["merchant_guild"]["tier_changed"] is False

    def test_reason_included_in_report(self):
        """Reason string is carried through to the change report."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10, reason="quest_reward")
        assert changes["merchant_guild"]["reason"] == "quest_reward"

    def test_reason_default_empty(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10)
        assert changes["merchant_guild"]["reason"] == ""


class TestPropagation:
    def test_allied_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10, faction_registry=FACTION_REGISTRY)
        # Ally gets +50% = trunc(10 * 0.5) = 5
        assert standings["artisan_collective"] == 5
        assert changes["artisan_collective"]["source"] == "allied_propagation"

    def test_rival_propagation(self):
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 10, faction_registry=FACTION_REGISTRY)
        # Rival gets -25% = trunc(10 * -0.25) = trunc(-2.5) = -2
        assert standings["thieves_den"] == -2
        assert changes["thieves_den"]["source"] == "rival_propagation"

    def test_rival_propagation_symmetric(self):
        """Gaining and losing equal amounts produces equal-magnitude rival propagation."""
        # Gain +10: rival gets trunc(10 * -0.25) = -2
        standings_gain: dict[str, int] = {}
        apply_standing_change(standings_gain, "merchant_guild", 10, faction_registry=FACTION_REGISTRY)

        # Lose -10: rival gets trunc(-10 * -0.25) = trunc(2.5) = 2
        standings_lose: dict[str, int] = {}
        apply_standing_change(standings_lose, "merchant_guild", -10, faction_registry=FACTION_REGISTRY)

        # Magnitudes should be equal (symmetric)
        assert abs(standings_gain["thieves_den"]) == abs(standings_lose["thieves_den"])

    def test_negative_delta_propagation(self):
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", -20, faction_registry=FACTION_REGISTRY)
        # Direct: 0 + (-20) = -20
        assert standings["merchant_guild"] == -20
        # Ally: trunc(-20 * 0.5) = -10
        assert standings["artisan_collective"] == -10
        # Rival: trunc(-20 * -0.25) = trunc(5) = 5
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
        """With trunc rounding, delta=1 produces zero for both ally and rival."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "merchant_guild", 1, faction_registry=FACTION_REGISTRY)
        # Ally: trunc(1 * 0.5) = trunc(0.5) = 0 → skipped
        assert "artisan_collective" not in changes
        assert standings.get("artisan_collective", 0) == 0
        # Rival: trunc(1 * -0.25) = trunc(-0.25) = 0 → skipped
        assert "thieves_den" not in changes
        assert standings.get("thieves_den", 0) == 0

    def test_propagated_changes_include_tier_transition(self):
        """Propagated changes also track tier transitions."""
        standings: dict[str, int] = {}
        # Delta 30: ally gets trunc(30*0.5)=15, 0→15 = neutral→friendly
        changes = apply_standing_change(standings, "merchant_guild", 30, faction_registry=FACTION_REGISTRY)
        ally = changes["artisan_collective"]
        assert ally["old_tier"] == "neutral"
        assert ally["tier"] == "friendly"
        assert ally["tier_changed"] is True

    def test_reason_propagated_to_all_changes(self):
        """Reason is included in propagated change entries."""
        standings: dict[str, int] = {}
        changes = apply_standing_change(
            standings, "merchant_guild", 10, faction_registry=FACTION_REGISTRY, reason="quest_complete"
        )
        for _fid, data in changes.items():
            assert data["reason"] == "quest_complete"


class TestPhantomStandingPrevention:
    """Propagation skips ally/rival references not in the registry."""

    def test_unknown_ally_not_created(self):
        registry = {
            "test_faction": {
                "id": "test_faction",
                "name": "Test",
                "allied_factions": ["ghost_faction"],
                "rival_factions": [],
            },
        }
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "test_faction", 20, faction_registry=registry)
        # ghost_faction is in allied_factions but not in registry → skipped
        assert "ghost_faction" not in standings
        assert "ghost_faction" not in changes

    def test_unknown_rival_not_created(self):
        registry = {
            "test_faction": {
                "id": "test_faction",
                "name": "Test",
                "allied_factions": [],
                "rival_factions": ["ghost_faction"],
            },
        }
        standings: dict[str, int] = {}
        changes = apply_standing_change(standings, "test_faction", 20, faction_registry=registry)
        assert "ghost_faction" not in standings
        assert "ghost_faction" not in changes


class TestPropagationCap:
    """Propagated deltas are capped at +-_MAX_PROPAGATED_DELTA."""

    def test_large_ally_delta_capped(self):
        """A +80 direct change caps ally propagation at +20 (not +40)."""
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", 80, faction_registry=FACTION_REGISTRY)
        # Without cap: trunc(80 * 0.5) = 40
        # With cap: min(40, 20) = 20
        assert standings["artisan_collective"] == _MAX_PROPAGATED_DELTA

    def test_large_rival_delta_capped(self):
        """A +100 direct change caps rival propagation at -20 (not -25)."""
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", 100, faction_registry=FACTION_REGISTRY)
        # Without cap: trunc(100 * -0.25) = -25
        # With cap: max(-25, -20) = -20
        assert standings["thieves_den"] == -_MAX_PROPAGATED_DELTA

    def test_negative_large_delta_caps_ally(self):
        """A -80 direct change caps ally propagation at -20 (not -40)."""
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", -80, faction_registry=FACTION_REGISTRY)
        assert standings["artisan_collective"] == -_MAX_PROPAGATED_DELTA

    def test_moderate_delta_not_capped(self):
        """Deltas that produce propagation within cap are unaffected."""
        standings: dict[str, int] = {}
        apply_standing_change(standings, "merchant_guild", 20, faction_registry=FACTION_REGISTRY)
        # trunc(20 * 0.5) = 10, under cap of 20
        assert standings["artisan_collective"] == 10


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
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=-30)
        # -30 → unfriendly (-50..-11) → +25% → 100 * 1.25 = 125
        assert price == 125

    def test_allied_sell_price(self):
        price = compute_sell_price(base_value=100, faction_standing=60)
        # allied → sell_back 0.5 + 0.10 = 0.60 → 60
        assert price == 60

    def test_unfriendly_sell_price(self):
        price = compute_sell_price(base_value=100, faction_standing=-30)
        # -30 → unfriendly (-50..-11) → sell_back 0.5 - 0.10 = 0.40 → 40
        assert price == 40

    def test_markup_with_faction_modifier(self):
        price = compute_buy_price(base_value=100, markup_pct=10.0, faction_standing=60)
        # 100 * 1.10 (markup) * 0.80 (allied) = 88
        assert price == 88


# ---------------------------------------------------------------------------
# Integration tests — endpoints
# ---------------------------------------------------------------------------


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
        assert direct["old_tier"] == "neutral"
        assert direct["tier_changed"] is True  # 0 (neutral) → 15 (friendly)

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
        assert direct["tier"] == "friendly"  # 20 >= 11 → friendly

        ally = changes_by_id["artisan_collective"]
        assert ally["new"] == 10  # 50% of 20
        assert ally["source"] == "allied_propagation"
        assert ally["tier"] == "neutral"  # 10 is neutral (-10..10)

        rival = changes_by_id["thieves_den"]
        assert rival["new"] == -5  # trunc(20 * -0.25) = -5
        assert rival["source"] == "rival_propagation"
        assert rival["tier"] == "neutral"  # -5 is neutral (-10..10)

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
        assert mg["tier"] == "friendly"  # 30 >= 11 → friendly

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
            "neutral": -10,
            "friendly": 11,
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
            "neutral": -20,
            "friendly": 5,
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
        # Ally gets trunc(50 * 0.5) = 25 (over cap of 20 → 20)
        # Lenient thresholds: 20 >= 20 → allied
        assert changes["lenient_guild"]["new"] == 20
        assert changes["lenient_guild"]["tier"] == "allied"

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
# Ally/rival overlap validation (#3)
# ---------------------------------------------------------------------------


class TestFactionOverlapValidation:
    def test_overlap_raises_validation_error(self):
        """A faction cannot have the same ID in both allied and rival lists."""
        with pytest.raises(ValidationError, match="allied and rival"):
            Faction(
                id="bad_faction",
                name="Bad",
                allied_factions=["shared_faction"],
                rival_factions=["shared_faction"],
                reputation_thresholds={
                    "hostile": -51,
                    "unfriendly": -50,
                    "neutral": -10,
                    "friendly": 11,
                    "allied": 51,
                },
                description="Should fail",
            )

    def test_no_overlap_passes(self):
        """Distinct ally/rival lists pass validation."""
        faction = Faction(
            id="good_faction",
            name="Good",
            allied_factions=["ally_a"],
            rival_factions=["rival_b"],
            reputation_thresholds={
                "hostile": -51,
                "unfriendly": -50,
                "neutral": -10,
                "friendly": 11,
                "allied": 51,
            },
            description="Should pass",
        )
        assert faction.id == "good_faction"


# ---------------------------------------------------------------------------
# updated_at consistency (#4)
# ---------------------------------------------------------------------------


class TestCustomThresholdPricing:
    """Pricing engine uses per-faction custom thresholds when provided."""

    def test_custom_thresholds_change_buy_price(self):
        """Standing 55 is 'allied' by default (-20%) but 'friendly' (-10%) with strict thresholds."""
        strict = {"hostile": -30, "unfriendly": -29, "neutral": -10, "friendly": 11, "allied": 70}
        # Default thresholds: 55 >= 51 → allied → 80
        default_price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=55)
        assert default_price == 80

        # Custom strict thresholds: 55 < 70 → friendly → 90
        custom_price = compute_buy_price(
            base_value=100, markup_pct=0.0, faction_standing=55, reputation_thresholds=strict
        )
        assert custom_price == 90

    def test_custom_thresholds_change_sell_price(self):
        """Sell-back also respects custom thresholds."""
        strict = {"hostile": -30, "unfriendly": -29, "neutral": -10, "friendly": 11, "allied": 70}
        # Default: 55 → allied → 0.50 + 0.10 = 60
        default_sell = compute_sell_price(base_value=100, faction_standing=55)
        assert default_sell == 60

        # Custom strict: 55 → friendly → 0.50 + 0.05 = 55
        custom_sell = compute_sell_price(base_value=100, faction_standing=55, reputation_thresholds=strict)
        assert custom_sell == 55

    def test_shop_price_modifiers_override_global(self):
        """Per-faction shop_price_modifiers override the global _FACTION_BUY_MODIFIER."""
        # Custom modifiers: allied pays 85% instead of default 80%
        modifiers = {"allied": 0.85, "friendly": 0.95}
        price = compute_buy_price(
            base_value=100,
            markup_pct=0.0,
            faction_standing=60,
            shop_price_modifiers=modifiers,
        )
        # 100 * 1.0 * 0.85 = 85
        assert price == 85

    def test_shop_price_modifiers_fallback_to_global(self):
        """Tiers not in shop_price_modifiers fall back to global table."""
        modifiers = {"allied": 0.85}  # Only allied overridden
        price = compute_buy_price(
            base_value=100,
            markup_pct=0.0,
            faction_standing=25,  # friendly
            shop_price_modifiers=modifiers,
        )
        # friendly not in modifiers → global: 1.0 + (-0.10) = 0.90 → 90
        assert price == 90

    def test_is_hostile_respects_custom_thresholds(self):
        """is_hostile uses custom thresholds when provided."""
        from relay.economy.pricing import is_hostile

        lenient = {"hostile": -80, "unfriendly": -79, "neutral": -20, "friendly": 5, "allied": 20}
        # Standing -60: default → hostile (< -50); lenient → unfriendly (-79 threshold)
        assert is_hostile(faction_standing=-60) is True
        assert is_hostile(faction_standing=-60, reputation_thresholds=lenient) is False


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
