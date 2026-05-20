"""Tests for the economy system: wallet, shop buy/sell, pricing, transactions.

Covers the full buy→sell round-trip with faction price modifiers and
verifies wallet changes match expected values from the design doc.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from relay.auth.tokens import create_account_token
from relay.economy.pricing import (
    compute_buy_price,
    compute_sell_price,
    faction_tier,
    is_hostile,
)
from relay.economy.shop import (
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
def character_id(db_client, auth_header):
    """Create a character with 500 gold in inkglass_dark."""
    resp = db_client.post(
        "/character",
        json={
            "world_id": "inkglass_dark",
            "name": "Kael",
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 10,
                "dexterity": 14,
                "constitution": 12,
                "intelligence": 12,
                "wisdom": 14,
                "charisma": 10,
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
    """Grant 500 gold to the test character.

    Uses the world-named currency ("gold" for inkglass_dark) to match
    the shop buy/sell logic which calls ``get_world_currency``.
    """
    resp = db_client.post(
        "/wallet/grant",
        json={
            "character_id": character_id,
            "currency": "gold",
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
    faction_id: str | None = None,
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
            knows=["local prices"],
            does_not_know=["magic"],
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
        faction_id=faction_id,
        world_position=WorldPosition(region_id=faction_region),
        ability_scores={
            "strength": 10,
            "dexterity": 10,
            "constitution": 10,
            "intelligence": 14,
            "wisdom": 12,
            "charisma": 14,
        },
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
        world_id="inkglass_dark",
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
        assert faction_tier(51) == "allied"
        assert faction_tier(100) == "allied"

    def test_friendly(self):
        assert faction_tier(1) == "friendly"
        assert faction_tier(50) == "friendly"

    def test_neutral(self):
        assert faction_tier(0) == "neutral"

    def test_unfriendly(self):
        assert faction_tier(-1) == "unfriendly"
        assert faction_tier(-50) == "unfriendly"

    def test_hostile(self):
        assert faction_tier(-51) == "hostile"
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
        """Allied standing gives 20% discount on buy price."""
        # Base 100, 10% markup = 110, then allied -20% = 110 * 0.80 = 88
        price = compute_buy_price(base_value=100, markup_pct=10.0, faction_standing=51)
        assert price == 88

    def test_friendly_discount(self):
        """Friendly standing gives 10% discount on buy price."""
        # Base 100, 0% markup = 100, then friendly -10% = 90
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=25)
        assert price == 90

    def test_unfriendly_surcharge(self):
        """Unfriendly standing adds 25% surcharge."""
        # Base 100, 0% markup = 100, then unfriendly +25% = 125
        price = compute_buy_price(base_value=100, markup_pct=0.0, faction_standing=-25)
        assert price == 125

    def test_minimum_price(self):
        """Price never drops below 1."""
        price = compute_buy_price(base_value=0, markup_pct=0.0, faction_standing=100)
        assert price >= 1

    def test_lookup_from_standings_dict(self):
        """Can look up faction standing from a character's dict."""
        price = compute_buy_price(
            base_value=100,
            markup_pct=0.0,
            faction_id="market_district",
            character_faction_standing={"market_district": 51},
        )
        # Allied: 100 * 0.80 = 80
        assert price == 80


class TestSellPrice:
    def test_neutral_default_ratio(self):
        """Neutral standing: 50% sell-back of base value."""
        price = compute_sell_price(base_value=100, faction_standing=0)
        assert price == 50

    def test_allied_bonus(self):
        """Allied: 50% + 10% = 60% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=51)
        assert price == 60

    def test_friendly_bonus(self):
        """Friendly: 50% + 5% = 55% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=25)
        assert price == 55

    def test_unfriendly_penalty(self):
        """Unfriendly: 50% - 10% = 40% sell-back."""
        price = compute_sell_price(base_value=100, faction_standing=-25)
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
        assert is_hostile(faction_standing=-51) is True
        assert is_hostile(faction_standing=-100) is True

    def test_not_hostile(self):
        assert is_hostile(faction_standing=0) is False
        assert is_hostile(faction_standing=-50) is False


# ---------------------------------------------------------------------------
# Unit tests: shop price quotes
# ---------------------------------------------------------------------------


class TestShopPriceQuotes:
    def test_get_shop_prices_basic(self):
        npc = _make_shop_npc()
        items = {
            "iron_sword": _make_item(item_id="iron_sword", value=30),
            "health_potion": _make_item(
                item_id="health_potion",
                name="Health Potion",
                item_type="consumable",
                value=20,
            ),
            "rare_amulet": _make_item(
                item_id="rare_amulet",
                name="Rare Amulet",
                item_type="armour",
                rarity="rare",
                value=2000,
            ),
        }

        # Simulate a character with neutral standing
        class FakeChar:
            faction_standing: dict = {}  # noqa: RUF012

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
        # Character starts with {"gold": 0} from create (get_world_currency maps inkglass_dark → gold)
        assert data["balances"]["gold"] == 0

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
    @patch("relay.endpoints.shop.load_item")
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
        assert wallet_resp.json()["balances"]["gold"] == 467

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
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
    @patch("relay.endpoints.shop.load_item")
    def test_buy_sell_roundtrip_prices(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
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
    @patch("relay.endpoints.shop.load_item")
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
    @patch("relay.endpoints.shop.load_item")
    def test_sell_item_not_in_inventory(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
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
    @patch("relay.endpoints.shop.load_item")
    def test_buy_legendary_blocked(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Legendary items cannot be purchased."""
        npc = _make_shop_npc(
            items=[
                {"item_id": "legendary_blade", "stock_quantity": 1, "markup_percentage": 0.0},
            ]
        )
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(
            item_id="legendary_blade",
            name="Legendary Blade",
            rarity="legendary",
            value=50000,
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "legendary_blade", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "legendary_cannot_purchase"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
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
    @patch("relay.endpoints.shop.load_item")
    def test_allied_faction_buy_discount(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
        """Allied faction gives 20% buy discount."""
        char_id = funded_character_id
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="iron_sword", value=30)

        # Set faction standing to allied (51+)
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
        # 30 * 1.10 markup * 0.80 allied = 26.4 → round = 26
        assert receipt["unit_price"] == 26
        assert receipt["faction_tier"] == "allied"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
    def test_allied_faction_sell_bonus(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
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
    @patch("relay.endpoints.shop.load_item")
    def test_hostile_faction_buy_refused(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
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
    @patch("relay.endpoints.shop.load_item")
    def test_unfriendly_surcharge_and_penalty(
        self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id
    ):
        """Unfriendly: +25% buy surcharge, 40% sell-back."""
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
        # 30 * 1.10 markup * 1.25 unfriendly = 41.25 → round = 41
        assert buy["unit_price"] == 41

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
    @patch("relay.endpoints.shop.load_item")
    def test_sell_bound_item_rejected(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Bound items cannot be sold."""
        char_id = funded_character_id
        npc = _make_shop_npc(
            items=[
                {"item_id": "quest_ring", "stock_quantity": 1, "markup_percentage": 0.0},
            ]
        )
        mock_load_npc.return_value = npc

        ring = _make_item(
            item_id="quest_ring",
            name="Quest Ring",
            rarity="common",
            value=50,
            binding="bind_on_acquire",
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
    @patch("relay.endpoints.shop.load_item")
    def test_item_not_in_shop(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Buying an item not in the shop's inventory returns 404."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(
            item_id="unknown_item",
            name="Mystery Box",
            value=999,
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


# ---------------------------------------------------------------------------
# Step 12 improvements — new tests
# ---------------------------------------------------------------------------


class TestWorldCurrency:
    """#7 — get_world_currency maps world_id to named currency."""

    def test_known_worlds(self):
        from relay.economy.shop import get_world_currency

        assert get_world_currency("inkglass_dark") == "gold"
        assert get_world_currency("murim") == "silver_taels"
        assert get_world_currency("cybernightlife") == "credits"

    def test_unknown_world_falls_back_to_world_id(self):
        from relay.economy.shop import get_world_currency

        assert get_world_currency("some_new_world") == "some_new_world"


class TestBindingAwareStacking:
    """#4 — _add_to_inventory stacks by item_id AND binding_state."""

    def test_unbound_stacks_with_unbound(self):
        from relay.economy.shop import _add_to_inventory

        item = _make_item(item_id="iron_sword", binding="unbound")
        inventory: list[dict] = [{"item_id": "iron_sword", "quantity": 2, "binding_state": "unbound"}]
        _add_to_inventory(inventory, item, 3)
        assert len(inventory) == 1
        assert inventory[0]["quantity"] == 5

    def test_bound_does_not_stack_with_unbound(self):
        from relay.economy.shop import _add_to_inventory

        item = _make_item(item_id="iron_sword", binding="bind_on_acquire")
        inventory: list[dict] = [{"item_id": "iron_sword", "quantity": 2, "binding_state": "unbound"}]
        _add_to_inventory(inventory, item, 1)
        # Should create a new stack, not merge
        assert len(inventory) == 2
        assert inventory[0]["binding_state"] == "unbound"
        assert inventory[0]["quantity"] == 2
        assert inventory[1]["binding_state"] == "bound"
        assert inventory[1]["quantity"] == 1


class TestNpcFactionId:
    """#8 — _npc_faction_id prefers explicit faction_id over region_id."""

    def test_explicit_faction_id(self):
        from relay.economy.shop import _npc_faction_id

        npc = _make_shop_npc(faction_region="market_district", faction_id="thieves_guild")
        assert _npc_faction_id(npc) == "thieves_guild"

    def test_falls_back_to_region_id(self):
        from relay.economy.shop import _npc_faction_id

        npc = _make_shop_npc(faction_region="market_district")
        assert _npc_faction_id(npc) == "market_district"


class TestPreferUnboundSell:
    """Sell logic should prefer unbound stacks over bound ones."""

    def test_find_inventory_entry_prefers_unbound(self):
        from relay.economy.shop import _find_inventory_entry

        inventory = [
            {"item_id": "ring", "quantity": 1, "binding_state": "bound"},
            {"item_id": "ring", "quantity": 3, "binding_state": "unbound"},
        ]
        entry = _find_inventory_entry(inventory, "ring", prefer_unbound=True)
        assert entry is not None
        assert entry["binding_state"] == "unbound"
        assert entry["quantity"] == 3

    def test_find_inventory_entry_falls_back_to_bound(self):
        from relay.economy.shop import _find_inventory_entry

        inventory = [
            {"item_id": "ring", "quantity": 1, "binding_state": "bound"},
        ]
        entry = _find_inventory_entry(inventory, "ring", prefer_unbound=True)
        assert entry is not None
        assert entry["binding_state"] == "bound"

    def test_remove_from_inventory_prefers_unbound(self):
        from relay.economy.shop import _remove_from_inventory

        inventory = [
            {"item_id": "ring", "quantity": 1, "binding_state": "bound"},
            {"item_id": "ring", "quantity": 3, "binding_state": "unbound"},
        ]
        _remove_from_inventory(inventory, "ring", 2)
        assert len(inventory) == 2
        assert inventory[0]["quantity"] == 1
        assert inventory[0]["binding_state"] == "bound"
        assert inventory[1]["quantity"] == 1
        assert inventory[1]["binding_state"] == "unbound"


class TestProduceOutputMutation:
    """#9 — produce_output always mutates in-place."""

    def test_stacking_returns_same_list(self):
        from relay.crafting.crafter import produce_output

        inventory: list[dict] = [{"item_id": "plank", "quantity": 3, "binding_state": "unbound"}]
        original_id = id(inventory)
        result = produce_output("plank", 2, inventory)
        # Must return the same list object (in-place mutation)
        assert id(result) == original_id
        assert len(result) == 1
        assert result[0]["quantity"] == 5

    def test_new_item_returns_same_list(self):
        from relay.crafting.crafter import produce_output

        inventory: list[dict] = [{"item_id": "plank", "quantity": 3, "binding_state": "unbound"}]
        original_id = id(inventory)
        result = produce_output("nail", 10, inventory)
        assert id(result) == original_id
        assert len(result) == 2


class TestQuestReward:
    """#10 — quest_reward helper creates a quest_reward transaction."""

    @pytest.mark.asyncio()
    async def test_quest_reward_credits_wallet(self):
        from unittest.mock import AsyncMock, MagicMock

        from relay.economy.wallet import quest_reward

        db = AsyncMock()
        db.add = MagicMock()
        char = MagicMock()
        char.wallet = {"gold": 100}
        char.player_id = "p1"
        char.id = "c1"
        char.world_id = "inkglass_dark"
        char.updated_at = None

        new_balance = await quest_reward(
            db,
            char,
            currency="gold",
            amount=50,
            quest_id="find_the_sword",
        )
        assert new_balance == 150
        assert char.wallet["gold"] == 150
        # Verify a TransactionLog was added
        db.add.assert_called_once()
        tx = db.add.call_args[0][0]
        assert tx.tx_type == "quest_reward"
        assert "find_the_sword" in tx.note


class TestShopBrowse:
    """#13 — GET /shop/{npc_id} browse endpoint tests."""

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
    def test_browse_shop(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Browse returns paginated item list with prices."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        potion = _make_item(item_id="health_potion", name="Health Potion", item_type="consumable", value=20)
        amulet = _make_item(item_id="rare_amulet", name="Rare Amulet", item_type="armour", rarity="rare", value=2000)

        async def _fake_load(item_id, world_id):
            return {"iron_sword": sword, "health_potion": potion, "rare_amulet": amulet}.get(item_id)

        mock_load_item.side_effect = _fake_load

        resp = db_client.get(
            f"/shop/merchant_001?character_id={funded_character_id}",
            headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["npc_id"] == "merchant_001"
        assert data["npc_name"] == "Gareth the Merchant"
        assert data["total"] == 3
        assert len(data["items"]) == 3

        sword_item = next(i for i in data["items"] if i["item_id"] == "iron_sword")
        assert sword_item["buy_price"] == 33
        assert sword_item["sell_price"] == 15
        assert sword_item["stock"] == 5

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
    def test_browse_pagination(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Pagination with limit and offset works."""
        npc = _make_shop_npc()
        mock_load_npc.return_value = npc

        sword = _make_item(item_id="iron_sword", value=30)
        potion = _make_item(item_id="health_potion", name="Health Potion", item_type="consumable", value=20)
        amulet = _make_item(item_id="rare_amulet", name="Rare Amulet", item_type="armour", rarity="rare", value=2000)

        async def _fake_load(item_id, world_id):
            return {"iron_sword": sword, "health_potion": potion, "rare_amulet": amulet}.get(item_id)

        mock_load_item.side_effect = _fake_load

        resp = db_client.get(
            f"/shop/merchant_001?character_id={funded_character_id}&limit=1&offset=1",
            headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 1


class TestOutOfStock:
    """#14 — OutOfStock error when shop has zero stock."""

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
    def test_buy_zero_stock_item(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Buying an item with stock_quantity=0 returns 409."""
        npc = _make_shop_npc(
            items=[
                {"item_id": "rare_gem", "stock_quantity": 0, "markup_percentage": 0.0},
            ]
        )
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(item_id="rare_gem", name="Rare Gem", rarity="rare", value=500)

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "rare_gem", "quantity": 1},
            headers=auth_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "out_of_stock"

    @patch("relay.endpoints.shop.load_npc")
    @patch("relay.endpoints.shop.load_item")
    def test_buy_exceeds_stock(self, mock_load_item, mock_load_npc, db_client, auth_header, funded_character_id):
        """Buying more than available stock returns 409."""
        npc = _make_shop_npc(
            items=[
                {"item_id": "health_potion", "stock_quantity": 2, "markup_percentage": 0.0},
            ]
        )
        mock_load_npc.return_value = npc
        mock_load_item.return_value = _make_item(
            item_id="health_potion", name="Health Potion", item_type="consumable", value=20
        )

        resp = db_client.post(
            "/shop/merchant_001/buy",
            json={"character_id": funded_character_id, "item_id": "health_potion", "quantity": 5},
            headers=auth_header,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "out_of_stock"


class TestWalletKeyInitialisation:
    """Character wallet key uses get_world_currency, not raw world_id."""

    def test_inkglass_dark_wallet_key_is_gold(self, db_client, auth_header, character_id):
        resp = db_client.get(f"/wallet/{character_id}", headers=auth_header)
        assert resp.status_code == 200
        balances = resp.json()["balances"]
        assert "gold" in balances
        assert "inkglass_dark" not in balances


class TestTransactionPagination:
    """Offset, limit, and total in transaction history."""

    def test_pagination_offset_limit(self, db_client, auth_header, character_id):
        for i in range(5):
            db_client.post(
                "/wallet/grant",
                json={"character_id": character_id, "currency": "gold", "amount": (i + 1) * 10},
                headers=auth_header,
            )

        resp = db_client.get(
            f"/wallet/{character_id}/transactions?limit=2&offset=1",
            headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["transactions"]) == 2

    def test_total_field_present(self, db_client, auth_header, character_id):
        resp = db_client.get(
            f"/wallet/{character_id}/transactions",
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert "total" in resp.json()
        assert resp.json()["total"] == 0


class TestTransactionTypeFilter:
    """tx_type query parameter filters transactions."""

    def test_filter_by_tx_type(self, db_client, auth_header, funded_character_id):
        char_id = funded_character_id
        # funded_character_id created one grant transaction

        resp = db_client.get(
            f"/wallet/{char_id}/transactions?tx_type=grant",
            headers=auth_header,
        )
        assert resp.status_code == 200
        txns = resp.json()["transactions"]
        assert len(txns) == 1
        assert all(t["tx_type"] == "grant" for t in txns)

    def test_filter_returns_empty_for_nonexistent_type(self, db_client, auth_header, funded_character_id):
        resp = db_client.get(
            f"/wallet/{funded_character_id}/transactions?tx_type=sell",
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert len(resp.json()["transactions"]) == 0


class TestTransactionEntryFields:
    """TransactionEntry includes all audit fields."""

    def test_full_fields_present(self, db_client, auth_header, character_id):
        db_client.post(
            "/wallet/grant",
            json={"character_id": character_id, "currency": "gold", "amount": 100, "note": "Test"},
            headers=auth_header,
        )

        resp = db_client.get(
            f"/wallet/{character_id}/transactions",
            headers=auth_header,
        )
        tx = resp.json()["transactions"][0]
        for field in ["session_id", "quest_id", "base_price", "markup_pct", "faction_modifier", "sell_back_ratio"]:
            assert field in tx


class TestQuestRewardStructuredQuestId:
    """quest_reward passes quest_id as a structured column, not just in note."""

    @pytest.mark.asyncio()
    async def test_quest_id_in_transaction(self):
        from unittest.mock import AsyncMock, MagicMock

        from relay.economy.wallet import quest_reward

        db = AsyncMock()
        db.add = MagicMock()
        char = MagicMock()
        char.wallet = {"gold": 100}
        char.player_id = "p1"
        char.id = "c1"
        char.world_id = "inkglass_dark"
        char.updated_at = None

        await quest_reward(
            db,
            char,
            currency="gold",
            amount=50,
            quest_id="find_the_sword",
        )

        tx = db.add.call_args[0][0]
        assert tx.quest_id == "find_the_sword"
        assert tx.tx_type == "quest_reward"


class TestGetBalanceSync:
    """get_balance is now sync — no await needed."""

    def test_get_balance_returns_int(self):
        from unittest.mock import MagicMock

        from relay.economy.wallet import get_balance

        char = MagicMock()
        char.wallet = {"gold": 250}
        assert get_balance(char, "gold") == 250
        assert get_balance(char, "missing") == 0


class TestNpcFactionIdNone:
    """_npc_faction_id returns None when NPC has no faction_id and no world_position."""

    def test_returns_none_for_no_faction_no_position(self):
        from relay.economy.shop import _npc_faction_id

        npc = _make_shop_npc(faction_region="market_district")
        # Remove world_position to simulate NPC with no location data
        object.__setattr__(npc, "world_position", None)
        object.__setattr__(npc, "faction_id", None)
        assert _npc_faction_id(npc) is None

    def test_neutral_pricing_when_no_faction(self):
        """NPC without faction should yield neutral pricing."""
        npc = _make_shop_npc(faction_region="market_district")
        object.__setattr__(npc, "world_position", None)
        object.__setattr__(npc, "faction_id", None)

        items = {"iron_sword": _make_item(item_id="iron_sword", value=100)}

        class FakeChar:
            faction_standing: dict = {"some_faction": 80}  # noqa: RUF012

        quotes = get_shop_prices(npc=npc, items=items, character=FakeChar())
        assert len(quotes) == 1
        # Should be neutral (no faction → standing 0 → neutral) with 10% markup
        assert quotes[0].faction_tier == "neutral"
        assert quotes[0].buy_price == 110  # 100 * 1.10 * 1.0


class TestSellBackRatioOverride:
    """World-specific sell_back_ratio is threaded through shop operations."""

    def test_custom_sell_back_ratio_in_quotes(self):
        npc = _make_shop_npc()
        items = {"iron_sword": _make_item(item_id="iron_sword", value=100)}

        class FakeChar:
            faction_standing: dict = {}  # noqa: RUF012

        # Default ratio (0.50) → sell price = 50
        quotes_default = get_shop_prices(npc=npc, items=items, character=FakeChar())
        assert quotes_default[0].sell_price == 50

        # Custom ratio (0.70) → sell price = 70
        quotes_custom = get_shop_prices(npc=npc, items=items, character=FakeChar(), sell_back_ratio=0.70)
        assert quotes_custom[0].sell_price == 70

    def test_zero_sell_back_ratio(self):
        """A world with 0% sell-back yields 0 sell price."""
        npc = _make_shop_npc()
        items = {"iron_sword": _make_item(item_id="iron_sword", value=100)}

        class FakeChar:
            faction_standing: dict = {}  # noqa: RUF012

        quotes = get_shop_prices(npc=npc, items=items, character=FakeChar(), sell_back_ratio=0.0)
        assert quotes[0].sell_price == 0


class TestReputationThresholdsValidation:
    """Pydantic model_validator enforces threshold ordering."""

    def test_valid_thresholds_pass(self):
        from relay.schemas import ReputationThresholds

        rt = ReputationThresholds(hostile=-51, unfriendly=-50, neutral=0, friendly=1, allied=51)
        assert rt.hostile == -51

    def test_invalid_ordering_raises(self):
        from pydantic import ValidationError

        from relay.schemas import ReputationThresholds

        with pytest.raises(ValidationError, match="must be ordered"):
            ReputationThresholds(hostile=10, unfriendly=5, neutral=0, friendly=1, allied=51)

    def test_hostile_equal_to_unfriendly_invalid(self):
        from pydantic import ValidationError

        from relay.schemas import ReputationThresholds

        with pytest.raises(ValidationError, match="must be ordered"):
            # hostile < unfriendly is required (strict), not <=
            ReputationThresholds(hostile=-50, unfriendly=-50, neutral=0, friendly=1, allied=51)

    def test_friendly_equal_to_allied_invalid(self):
        from pydantic import ValidationError

        from relay.schemas import ReputationThresholds

        with pytest.raises(ValidationError, match="must be ordered"):
            # friendly < allied is required (strict)
            ReputationThresholds(hostile=-51, unfriendly=-50, neutral=0, friendly=51, allied=51)

    def test_neutral_boundaries_allow_equality(self):
        """unfriendly <= neutral and neutral <= friendly are allowed."""
        from relay.schemas import ReputationThresholds

        rt = ReputationThresholds(hostile=-51, unfriendly=0, neutral=0, friendly=0, allied=51)
        assert rt.neutral == 0


class TestShopPriceModifiersSchema:
    """ShopPriceModifiers Pydantic model validation."""

    def test_valid_modifiers(self):
        from relay.schemas import ShopPriceModifiers

        mods = ShopPriceModifiers(allied=0.80, friendly=0.90)
        assert mods.allied == 0.80
        assert mods.neutral is None

    def test_extra_fields_rejected(self):
        from relay.schemas import ShopPriceModifiers

        with pytest.raises(Exception):  # noqa: B017
            ShopPriceModifiers(allied=0.80, legendary=0.50)


class TestGatherTransactionLog:
    """#11 — successful gather creates a transaction log entry."""

    def test_gather_creates_transaction(
        self,
        db_client,
        auth_header,
        session_header,
        character_id,
    ):
        # Mock: first call is the d20 check roll (passes DC 10),
        # second call is the yield randint(1, 3) → 2.
        with patch("relay.checks.resolver.random.randint", side_effect=[15, 2]):
            resp = db_client.post(
                "/gather",
                json={
                    "character_id": character_id,
                    "region_id": "thornveil_lowlands",
                    "node_item_id": "camphor_resin",
                },
                headers=session_header,
            )

        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["quantity"] == 2

        # Verify transaction log has a gather entry
        tx_resp = db_client.get(
            f"/wallet/{character_id}/transactions",
            headers=auth_header,
        )
        txns = tx_resp.json()["transactions"]
        gather_txns = [t for t in txns if t["tx_type"] == "gather"]
        assert len(gather_txns) == 1
        assert gather_txns[0]["item_id"] == "camphor_resin"
        assert gather_txns[0]["item_quantity"] == 2
