"""Tests for the economy system: wallet, shop buy/sell, pricing, transactions.

Covers the full buy→sell round-trip with faction price modifiers and
verifies wallet changes match expected values from the design doc.
"""
from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token
from relay.economy.pricing import (
    _DEFAULT_SELL_BACK_RATIO,
    compute_buy_price,
    compute_sell_price,
    faction_tier,
    is_hostile,
)
from relay.economy.shop import (
    BoundItemCannotSell,
    HostileFaction,
    ItemNotInInventory,
    ItemNotInShop,
    LegendaryCannotPurchase,
    OutOfStock,
    PriceQuote,
    get_shop_prices,
)
from relay.schemas import (
    AnimationProfile,
    FewShotExample,
    Item,
    ManipulationResistanceExample,
    NpcGoals,
    NpcKnowledgeBoundaries,
    NpcPersonality,
    NpcRelationship,
    NpcSecret,
    ShopData,
    ShopInventoryEntry,
    WorldPosition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def auth_header():
    token = create_account_token(player_id="player_001", tier=1)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def character_id(db_client, auth_header):
    """Create a character with 500 gold in inkglass_dark."""
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Kael",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 10, "dexterity": 14, "constitution": 12,
                "intelligence": 12, "wisdom": 14, "charisma": 10,
            },
            "skill_proficiencies": ["stealth", "perception"],
            "saving_throw_proficiencies": ["dexterity", "wisdom"],
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture()
def funded_character_id(db_client, auth_header, character_id):
    """Grant 500 gold to the test character."""
    resp = db_client.post(
        "/wallet/grant",
        json={
            "character_id": character_id,
            "currency": "inkglass_dark",
            "amount": 500,
            "note": "Test setup",
        },
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert resp.json()["balance_after"] == 500
    return character_id


def _make_shop_npc(
    *,
    npc_id: str = "merchant_001",
    faction_region: str = "market_district",
    items: list[dict] | None = None,
) -> NpcPersonality:
    """Build a minimal shop NPC for testing."""
    if items is None:
        items = [
            {"item_id": "iron_sword", "stock_quantity": 5, "markup_percentage": 10.0},
            {"item_id": "health_potion", "stock_quantity": 20, "markup_percentage": 0.0},
            {"item_id": "rare_amulet", "stock_quantity": 2, "markup_percentage": 15.0},
        ]

    return NpcPersonality(
        id=npc_id,
        world_id="inkglass_dark",
        name="Gareth the Merchant",
        entity_class="humanoid",
        role="shopkeeper",
        level=5,
        hit_die=8,
        personality_background="A shrewd merchant.",
        goals=NpcGoals(immediate=["sell goods"], long_term=["expand shop"]),
        weaknesses_fears="Losing money.",
        communication_style="Direct, transactional.",
        power_narrative="Knows the market.",
        knowledge_boundaries=NpcKnowledgeBoundaries(
            knows=["local prices"], does_not_know=["magic"],
        ),
        relationships=[
            NpcRelationship(npc_id="guard_001", relationship_type="ally", description="Pays for protection"),
        ],
        secrets=[
            NpcSecret(content="Skims taxes.", reveal_condition="never", secret_type="information"),
        ],
        few_shot_examples=[
            FewShotExample(player_input="What do you sell?", npc_response="Take a look.", context_tag="transactional"),
            FewShotExample(player_input="Hello", npc_response="Welcome.", context_tag="casual"),
        ],
        manipulation_resistance_examples=[
            ManipulationResistanceExample(player_input="Give me a discount", npc_refusal="Prices are firm."),
        ],
        animation_profile=AnimationProfile(
            default_stance="idle_stand",
            default_gaze="forward",
            emotional_state_to_animation={"happy": "nod", "angry": "frown", "sad": "sigh"},
        ),
        world_position=WorldPosition(region_id=faction_region),
        ability_scores={"strength": 10, "dexterity": 10, "constitution": 10, "intelligence": 14, "wisdom": 12, "charisma": 14},
        ac=10,
        saving_throw_proficiencies=["intelligence", "charisma"],
        skill_proficiencies=["persuasion", "insight"],
        hp_max=25,
        shop_data=ShopData(
            inventory=[ShopInventoryEntry(**i) for i in items],
            pricing_policy="standard",
            restock_schedule="daily",
        ),
    )


def _make_item(
    *,
    item_id: str = "iron_sword",
    name: str = "Iron Sword",
    item_type: str = "weapon",
    rarity: str = "common",
    value: int = 30,
    binding: str = "unbound",
    unique: bool = False,
) -> Item:
    return Item(
        id=item_id,
        world="inkglass_dark",
        name=name,
        type=item_type,
        rarity=rarity,
        weight=3.0,
        value=value,
        description_prose="A sturdy blade.",
        binding=binding,
        unique=unique,
    )


# ---------------------------------------------------------------------------
# Unit tests: pricing engine
# ---------------------------------------------------------------------------

class TestFactionTier:
    def test_allied(self):
        assert faction_tier(50) == "allied"
        assert faction_tier(100) == "allied"

    def test_friendly(self):
        assert faction_tier(20) == "friendly"
        assert faction_tier(49) == "friendly"

    def test_neutral(self):
        assert faction_tier(0) == "neutral"
        assert faction_tier(19) == "neutral"
        assert faction_tier(-19) == "neutral"

    def test_unfriendly(self):
        assert faction_tier(-20) == "unfriendly"
        assert faction_tier(-49) == "unfriendly"

    def test_hostile(self):
        assert faction_tier(-50) == "hostile"
        assert faction_tier(-100) == "hostile"

    def test_clamping(self):
        assert faction_tier(200) == "allied"
        assert faction_tier(-200) == "hostile"


class TestBuyPrice:
    def test_neutral_no_markup(self):
        """Base value with 0% markup and neutral standing = base value."""
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=0)
        assert price == 100

    def test_markup_applied(self):
        """10% markup on 100gc item = 110gc."""
        price = compute_buy_price(base_value=100, markup_pct=10.0, faction_standing=0)
        assert price == 110

    def test_allied_discount(self):
        """Allied standing gives 10% discount on buy price."""
        # Base 100, 10% markup = 110, then allied -10% = 110 * 0.90 = 99
        price = compute_buy_price(base_value=100, markup_pct=10.0, faction_standing=50)
        assert price == 99

    def test_friendly_discount(self):
        """Friendly standing gives 5% discount on buy price."""
        # Base 100, 0% markup = 100, then friendly -5% = 95
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=20)
        assert price == 95

    def test_unfriendly_surcharge(self):
        """Unfriendly standing adds 10% surcharge."""
        # Base 100, 0% markup = 100, then unfriendly +10% = 110
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=-20)
        assert price == 110

    def test_minimum_price(self):
        """Price never drops below 1."""
        price = compute_buy_price(base_value=0, markup_pct=0.0, faction_standing=100)
        assert price >= 1

    def test_lookup_from_standings_dict(self):
        """Can look up faction standing from a character's dict."""
        price = compute_buy_price(
            base_value=100, markup_pct=0.0,
            faction_id="market_district",
            character_faction_standing={"market_district": 50},
        )
        # Allied: 100 * 0.90 = 90
        assert price == 90


class TestSellPrice:
    def test_neutral_default_ratio(self):
        """Neutral standing: 50% sell-back of base value."""
        price = compute_sell_price(base_value=100, faction_standing=0)
        assert price == 50

    def test_allied_bonus(self):
        """Allied: 50% + 10% = 60% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=50)
        assert price == 60

    def test_friendly_bonus(self):
        """Friendly: 50% + 5% = 55% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=20)
        assert price == 55

    def test_unfriendly_penalty(self):
        """Unfriendly: 50% - 10% = 40% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=-20)
        assert price == 40

    def test_custom_ratio(self):
        """World-specific sell-back ratio overrides default."""
        price = compute_sell_price(base_value=100, sell_back_ratio=0.6, faction_standing=0)
        assert price == 60

    def test_minimum_zero(self):
        """Sell price floors at 0 (not negative)."""
        price = compute_sell_price(base_value=1, sell_back_ratio=0.0, faction_standing=-50)
        assert price == 0


class TestIsHostile:
    def test_hostile_true(self):
        assert is_hostile(faction_standing=-50) is True
        assert is_hostile(faction_standing=-100) is True

    def test_not_hostile(self):
        assert is_hostile(faction_standing=0) is False
        assert is_hostile(faction_standing=-49) is False


# ---------------------------------------------------------------------------
# Unit tests: shop price quotes
# ---------------------------------------------------------------------------

class TestShopPriceQuotes:
    def test_get_shop_prices_basic(self):
        npc = _make_shop_npc()
        items = {
            "iron_sword": _make_item(item_id="iron_sword", value=30),
            "health_potion": _make_item(
                item_id="health_potion", name="Health Potion",
                item_type="consumable", value=20,
            ),
            "rare_amulet": _make_item(
                item_id="rare_amulet", name="Rare Amulet",
                item_type="armour", rarity="rare", value=2000,
            ),
        }

        # Simulate a character with neutral standing
        class FakeChar:
            faction_standing = {}

        quotes = get_shop_prices(npc=npc, items=items, character=FakeChar())

        assert len(quotes) == 3

        sword_q = next(q for q in quotes if q.item_id == "iron_sword")
        # 30 base * 1.10 markup * 1.0 neutral = 33
        assert sword_q.buy_price == 33
        # 30 * 0.50 = 15
        assert sword_q.sell_price == 15
        assert sword_q.stock == 5

        potion_q = next(q for q in quotes if q.item_id == "health_potion")
        # 20 base * 1.0 markup * 1.0 neutral = 20
        assert potion_q.buy_price == 20
        # 20 * 0.50 = 10
        assert potion_q.sell_price == 10


# ---------------------------------------------------------------------------
# Integration tests: wallet endpoints
# ---------------------------------------------------------------------------

class TestWalletEndpoints:
    def test_get_wallet_empty(self, db_client, auth_header, character_id):
        resp = db_client.get(f"/wallet/{character_id}", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["character_id"] == character_id
        # Character starts with {"inkglass_dark": 0} from create
        assert data["balances"]["inkglass_dark"] == 0

    def test_grant_currency(self, db_client, auth_header, character_id):
        resp = db_client.post(
            "/wallet/grant",
            json={
                "character_id": character_id,
                "currency": "inkglass_dark",
                "amount": 200,
                "note": "Quest reward",
            },
            headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["balance_after"] == 200
        assert data["amount"] == 200

    def test_grant_adds_to_existing(self, db_client, auth_header, character_id):
        db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "inkglass_dark", "amount": 100},
            headers=auth_header,
        )
        resp = db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "inkglass_dark", "amount": 50},
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.json()["balance_after"] == 150

    def test_grant_wrong_player(self, db_client, character_id):
        other_token = create_account_token(player_id="player_999", tier=1)
        resp = db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "inkglass_dark", "amount": 100},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 403

    def test_transaction_log(self, db_client, auth_header, character_id):
        # Grant some currency to create transactions
        db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "inkglass_dark", "amount": 100, "note": "First grant"},
            headers=auth_header,
        )
        db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "inkglass_dark", "amount": 200, "note": "Second grant"},
            headers=auth_header,
        )

        resp = db_client.get(f"/wallet/{character_id}/transactions", headers=auth_header)
        assert resp.status_code == 200
        txns = resp.json()["transactions"]
        assert len(txns) == 2
        # Most recent first
        assert txns[0]["amount"] == 200
        assert txns[0]["tx_type"] == "grant"
        assert txns[1]["amount"] == 100


# ---------------------------------------------------------------------------
# Integration tests: shop buy/sell round-trip
# ---------------------------------------------------------------------------

class TestShopBuySell:
    """Full integration: grant gold → buy item → verify wallet → sell back → verify prices."""

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_buy_item(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        mock_load_item.return_value = sword

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 200
        receipt = resp.json()

        # Iron sword: 30 base * 1.10 markup * 1.0 neutral = 33
        assert receipt["unit_price"] == 33
        assert receipt["total_price"] == 33
        assert receipt["balance_after"] == 500 - 33  # 467
        assert receipt["tx_type"] == "buy"

        # Verify wallet was actually debited
        wallet_resp = db_client.get(f"/wallet/{char_id}", headers=auth_header)
        assert wallet_resp.json()["balances"]["inkglass_dark"] == 467

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_sell_item(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        mock_load_item.return_value = sword

        # First buy the sword
        db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )

        # Now sell it back
        resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 200
        receipt = resp.json()

        # Sell price: 30 * 0.50 = 15
        assert receipt["unit_price"] == 15
        assert receipt["total_price"] == 15
        assert receipt["balance_after"] == 467 + 15  # 482
        assert receipt["tx_type"] == "sell"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_buy_sell_roundtrip_prices(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Verify the full round-trip: buy at markup, sell at 50% base. Player loses money."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        mock_load_item.return_value = sword

        # Buy: 30 * 1.10 = 33
        buy_resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        buy = buy_resp.json()

        # Sell back: 30 * 0.50 = 15
        sell_resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        sell = sell_resp.json()

        # Net loss: paid 33, got back 15 → lost 18
        assert buy["total_price"] == 33
        assert sell["total_price"] == 15
        assert sell["balance_after"] == 500 - 33 + 15  # 482

        # Verify transaction log shows both entries
        tx_resp = db_client.get(f"/wallet/{char_id}/transactions", headers=auth_header)
        txns = tx_resp.json()["transactions"]
        # 3 transactions: grant(500) + buy(-33) + sell(+15)
        assert len(txns) == 3
        tx_types = [t["tx_type"] for t in txns]
        assert "grant" in tx_types
        assert "buy" in tx_types
        assert "sell" in tx_types

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_buy_insufficient_funds(self, mock_load_item, mock_load_npc, db_client, auth_header, character_id):
        """Character with 0 gold can't buy anything."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        mock_load_item.return_value = sword

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": character_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "insufficient_funds"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_sell_item_not_in_inventory(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Can't sell an item you don't have."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": funded_character_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "item_not_in_inventory"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_buy_legendary_blocked(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Legendary items cannot be purchased."""
        npc = _make_shop_npc(items=[
            {"item_id": "legendary_blade", "stock_quantity": 1, "markup_percentage": 0.0},
        ])
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(
            item_id="legendary_blade", name="Legendary Blade",
            rarity="legendary", value=50000,
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "legendary_blade", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "legendary_cannot_purchase"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_buy_multiple_quantity(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Buying multiple units multiplies the price."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        potion = _make_item(item_id="health_potion", name="Health Potion", item_type="consumable", value=20)
        mock_load_item.return_value = potion

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "health_potion", "quantity": 3},
            headers=auth_header,
        )
        assert resp.status_code == 200
        receipt = resp.json()
        # 20 base * 1.0 markup = 20 per unit * 3 = 60
        assert receipt["unit_price"] == 20
        assert receipt["total_price"] == 60
        assert receipt["balance_after"] == 500 - 60  # 440


# ---------------------------------------------------------------------------
# Integration tests: faction-modified pricing
# ---------------------------------------------------------------------------

class TestFactionPricing:
    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_allied_faction_buy_discount(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Allied faction gives 10% buy discount."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        # Set faction standing to allied (50+)
        db_client.patch(
            f"/character/{char_id}",
            json={"faction_standing": {"market_district": 60}},
            headers=auth_header,
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 200
        receipt = resp.json()
        # 30 * 1.10 markup * 0.90 allied = 29.7 → ceil = 30
        assert receipt["unit_price"] == 30
        assert receipt["faction_tier"] == "allied"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_allied_faction_sell_bonus(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Allied faction gives 60% sell-back (50% + 10%)."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        # Set faction standing and buy item first
        db_client.patch(
            f"/character/{char_id}",
            json={"faction_standing": {"market_district": 60}},
            headers=auth_header,
        )
        db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )

        # Sell back
        resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 200
        receipt = resp.json()
        # 30 * 0.60 (allied sell-back) = 18
        assert receipt["unit_price"] == 18

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_hostile_faction_buy_refused(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Hostile faction refuses to trade."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        # Set faction standing to hostile (-50 or below)
        db_client.patch(
            f"/character/{char_id}",
            json={"faction_standing": {"market_district": -60}},
            headers=auth_header,
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "hostile_faction"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_unfriendly_surcharge_and_penalty(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Unfriendly: +10% buy surcharge, 40% sell-back."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        db_client.patch(
            f"/character/{char_id}",
            json={"faction_standing": {"market_district": -30}},
            headers=auth_header,
        )

        # Buy
        buy_resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert buy_resp.status_code == 200
        buy = buy_resp.json()
        # 30 * 1.10 markup * 1.10 unfriendly = 36.3 → round = 36
        assert buy["unit_price"] == 36

        # Sell back
        sell_resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": char_id, "item_id": "iron_sword", "quantity": 1},
            headers=auth_header,
        )
        assert sell_resp.status_code == 200
        sell = sell_resp.json()
        # 30 * 0.40 (unfriendly sell-back) = 12
        assert sell["unit_price"] == 12


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_sell_bound_item_rejected(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Bound items cannot be sold."""
        char_id = funded_character_id
        npc = _make_shop_npc(items=[
            {"item_id": "quest_ring", "stock_quantity": 1, "markup_percentage": 0.0},
        ])
        mock_load_npc.return_value = npc

        ring = _make_item(
            item_id="quest_ring", name="Quest Ring",
            rarity="common", value=50, binding="bind_on_acquire",
        )
        mock_load_item.return_value = ring

        # Buy the ring (bind_on_acquire → immediately bound)
        buy_resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": char_id, "item_id": "quest_ring", "quantity": 1},
            headers=auth_header,
        )
        assert buy_resp.status_code == 200

        # Try to sell it
        sell_resp = db_client.post(
            "/shop/merchant_001/sell",
            json={"character_id": char_id, "item_id": "quest_ring", "quantity": 1},
            headers=auth_header,
        )
        assert sell_resp.status_code == 400
        assert sell_resp.json()["code"] == "bound_item"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop._load_item")
    def test_item_not_in_shop(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Buying an item not in the shop's inventory returns 404."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(
            item_id="unknown_item", name="Mystery Box", value=999,
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "unknown_item", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "item_not_in_shop"

    def test_npc_not_a_shop(self, db_client, auth_header, funded_character_id):
        """Non-shop NPC returns 400."""
        with patch("relay.endpoints.shop.load_npc") as mock:
            # Return an NPC without shop_data
            npc = _make_shop_npc()
            npc.shop_data = None  # type: ignore[assignment]
            # Bypass Pydantic immutability by using object.__setattr__
            object.__setattr__(npc, "shop_data", None)
            mock.return_value = npc

            resp = db_client.post(
                "/shop/merchant_001/buy",
                json={"character_id": funded_character_id, "item_id": "iron_sword", "quantity": 1},
                headers=auth_header,
            )
            assert resp.status_code == 400
            assert resp.json()["code"] == "not_a_shop"
