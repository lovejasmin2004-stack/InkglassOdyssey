"""What a player may and may not assert in combat, and who may be DM.

The shared ``db_client`` fixture runs with ADMIN_MODE on, which is how the older
combat tests pass free-form weapons. These tests set a scene up in admin mode,
then switch it off to act as an ordinary player.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import relay.config as _config
from relay.auth.tokens import create_account_token, create_session_token
from relay.combat.resolver import damage_roll, roll_dice

_WORLD = "wha_au"  # the only world with item files; Tier 2


def _account(player_id: str) -> dict:
    return {"Authorization": f"Bearer {create_account_token(player_id=player_id, tier=2)}"}


def _session(player_id: str, *, role: str = "player", session_id: str = "sess_combat") -> dict:
    mode = "multiplayer" if role == "dm" else "solo"
    token = create_session_token(
        player_id=player_id, world_id=_WORLD, session_id=session_id, tier=2, role=role, mode=mode
    )
    return {"Authorization": f"Bearer {token}"}


def _character(db_client, player_id: str, name: str, **patches) -> str:
    resp = db_client.post(
        "/character",
        json={
            "world_id": _WORLD,
            "name": name,
            "specialisation_path_id": "scout",
            "ability_scores": {
                "strength": 16,
                "dexterity": 14,
                "constitution": 14,
                "intelligence": 10,
                "wisdom": 12,
                "charisma": 8,
            },
            "skill_proficiencies": ["athletics"],
            "saving_throw_proficiencies": ["strength", "constitution"],
        },
        headers=_account(player_id),
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    resp = db_client.patch(
        f"/character/{cid}", json={"hp_max": 30, "hp_current": 30, "ac": 12, **patches}, headers=_account(player_id)
    )
    assert resp.status_code == 200, resp.text
    return cid


def _stack(item_id: str, quantity: int = 1) -> dict:
    return {"item_id": item_id, "quantity": quantity, "binding_state": "unbound"}


def _get(db_client, cid: str, player_id: str) -> dict:
    return db_client.get(f"/character/{cid}", headers=_account(player_id)).json()


@pytest.fixture()
def fighter(db_client) -> str:
    return _character(
        db_client, "p1", "Fighter", inventory=[_stack("iron_longsword"), _stack("healing_salve", 2)], hp_current=10
    )


@pytest.fixture()
def rival(db_client) -> str:
    return _character(db_client, "p2", "Rival")


@pytest.fixture()
def as_player(monkeypatch) -> None:
    """Switch ADMIN_MODE off. Request this after the fixtures that build characters."""
    monkeypatch.setattr(_config.settings, "admin_mode", False)


def _attack(db_client, attacker: str, target: str, headers: dict | None = None, **extra):
    return db_client.post(
        "/combat/attack",
        json={"attacker_id": attacker, "target_id": target, **extra},
        headers=headers or _session("p1"),
    )


class TestPlayerAttack:
    @patch("relay.combat.resolver.random.randint", return_value=5)
    @patch("relay.combat.resolver.roll_d20", return_value=(15, [15]))
    def test_held_weapon_uses_the_item_files_stats(self, _d20, _dmg, db_client, fighter, rival, as_player):
        resp = _attack(db_client, fighter, rival, weapon_item_id="iron_longsword")
        assert resp.status_code == 200, resp.text
        damage = resp.json()["damage"]
        assert damage["damage_dice"] == "1d8"
        assert damage["damage_type"] == "slashing"
        assert damage["raw_total"] == 5 + 3  # die + strength modifier
        assert resp.json()["target_hp_after"] == 30 - 8

    @patch("relay.combat.resolver.roll_d20", return_value=(15, [15]))
    def test_no_weapon_means_an_unarmed_strike(self, _d20, db_client, fighter, rival, as_player):
        resp = _attack(db_client, fighter, rival)
        assert resp.status_code == 200, resp.text
        damage = resp.json()["damage"]
        assert damage["damage_type"] == "bludgeoning"
        assert damage["raw_total"] == 1 + 3

    def test_weapon_the_character_does_not_hold_is_refused(self, db_client, fighter, rival, as_player):
        resp = _attack(db_client, rival, fighter, headers=_session("p2"), weapon_item_id="iron_longsword")
        assert resp.status_code == 400
        assert resp.json()["code"] == "item_not_held"

    def test_item_that_is_not_a_weapon_is_refused(self, db_client, fighter, rival, as_player):
        resp = _attack(db_client, fighter, rival, weapon_item_id="healing_salve")
        assert resp.status_code == 400
        assert resp.json()["code"] == "not_a_weapon"

    @pytest.mark.parametrize(
        "claim",
        [
            {"weapon": {"name": "Godslayer", "stats": {"damage_dice": "100d100", "damage_type": "force"}}},
            {"advantage": True},
            {"environmental_effects": ["high_ground"]},
        ],
        ids=["free-form weapon", "advantage", "environment"],
    )
    def test_player_cannot_assert_numbers(self, db_client, fighter, rival, as_player, claim):
        resp = _attack(db_client, fighter, rival, **claim)
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"
        assert _get(db_client, rival, "p2")["hp_current"] == 30

    def test_dm_can_still_use_a_free_form_weapon(self, db_client, fighter, rival, as_player):
        resp = _attack(
            db_client,
            fighter,
            rival,
            headers=_session("gm", role="dm"),
            weapon={"name": "Falling masonry", "stats": {"damage_dice": "2d6", "damage_type": "bludgeoning"}},
        )
        assert resp.status_code == 200, resp.text

    def test_environment_comes_from_the_scene(self, db_client, fighter, rival, monkeypatch):
        started = db_client.post(
            "/session/start", json={"character_id": fighter, "world_id": _WORLD}, headers=_account("p1")
        )
        assert started.status_code == 201, started.text
        session_token = {"Authorization": f"Bearer {started.json()['session_token']}"}
        scene = db_client.post(
            "/scene", json={"session_id": started.json()["session_id"], "npc_id": "seta"}, headers=_account("p1")
        )
        assert scene.status_code == 201, scene.text
        patched = db_client.patch(
            f"/scene/{scene.json()['id']}", json={"environmental_effects": ["darkness"]}, headers=session_token
        )
        assert patched.status_code == 200, patched.text

        monkeypatch.setattr(_config.settings, "admin_mode", False)
        resp = _attack(db_client, fighter, rival, headers=session_token)
        assert resp.status_code == 200, resp.text
        assert resp.json()["attack_roll"]["roll_mode"] == "disadvantage"


class TestPlayerHeal:
    def _heal(self, db_client, headers: dict | None = None, **body):
        return db_client.post("/combat/heal", json=body, headers=headers or _session("p1"))

    @patch("relay.combat.resolver.random.randint", return_value=3)
    def test_salve_heals_by_its_dice_and_is_used_up(self, _roll, db_client, fighter, as_player):
        resp = self._heal(db_client, target_id=fighter, item_id="healing_salve")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["healing"] == 3 + 3 + 2  # 2d4+2
        assert data["hp_after"] == 18
        assert data["item_consumed"] == "healing_salve"
        held = {e["item_id"]: e["quantity"] for e in _get(db_client, fighter, "p1")["inventory"]}
        assert held["healing_salve"] == 1

    def test_cannot_heal_once_the_salve_is_gone(self, db_client, fighter, as_player):
        for _ in range(2):
            assert self._heal(db_client, target_id=fighter, item_id="healing_salve").status_code == 200
        resp = self._heal(db_client, target_id=fighter, item_id="healing_salve")
        assert resp.status_code == 400
        assert resp.json()["code"] == "item_not_held"

    def test_player_can_use_their_salve_on_someone_else(self, db_client, fighter, rival, as_player):
        resp = self._heal(db_client, target_id=rival, user_id=fighter, item_id="healing_salve")
        assert resp.status_code == 200, resp.text
        held = {e["item_id"]: e["quantity"] for e in _get(db_client, fighter, "p1")["inventory"]}
        assert held["healing_salve"] == 1

    def test_player_cannot_spend_another_players_salve(self, db_client, fighter, rival, as_player):
        resp = self._heal(db_client, headers=_session("p2"), target_id=rival, user_id=fighter, item_id="healing_salve")
        assert resp.status_code == 403

    def test_item_that_does_not_heal_is_refused_and_kept(self, db_client, fighter, as_player):
        resp = self._heal(db_client, target_id=fighter, item_id="iron_longsword")
        assert resp.status_code == 400
        assert resp.json()["code"] == "not_usable"
        assert "iron_longsword" in {e["item_id"] for e in _get(db_client, fighter, "p1")["inventory"]}

    def test_player_cannot_name_a_healing_amount(self, db_client, fighter, as_player):
        resp = self._heal(db_client, target_id=fighter, healing=999)
        assert resp.status_code == 403
        assert _get(db_client, fighter, "p1")["hp_current"] == 10

    @pytest.mark.parametrize("body", [{}, {"item_id": "healing_salve", "healing": 5}], ids=["neither", "both"])
    def test_exactly_one_source_of_healing(self, db_client, fighter, body):
        resp = self._heal(db_client, target_id=fighter, **body)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_request"


class TestForcedSave:
    def test_player_cannot_force_a_saving_throw(self, db_client, fighter, rival, as_player):
        resp = db_client.post(
            "/combat/save",
            json={
                "attacker_id": fighter,
                "defender_id": rival,
                "save_type": "dexterity",
                "dc_source_ability": "intelligence",
                "damage_dice": "100d100",
            },
            headers=_session("p1"),
        )
        assert resp.status_code == 403
        assert _get(db_client, rival, "p2")["hp_current"] == 30


class TestLongRest:
    def _rest(self, db_client, character: str, headers: dict, rest_type: str = "long"):
        return db_client.post("/combat/rest", json={"character_id": character, "rest_type": rest_type}, headers=headers)

    def test_one_long_rest_per_session(self, db_client, fighter, as_player):
        first = self._rest(db_client, fighter, _session("p1"))
        assert first.status_code == 200, first.text
        assert first.json()["hp_after"] == 30

        second = self._rest(db_client, fighter, _session("p1"))
        assert second.status_code == 409
        assert second.json()["code"] == "rest_unavailable"

    def test_a_new_session_allows_another(self, db_client, fighter, as_player):
        assert self._rest(db_client, fighter, _session("p1")).status_code == 200
        assert self._rest(db_client, fighter, _session("p1", session_id="sess_next")).status_code == 200

    def test_short_rests_are_not_limited(self, db_client, fighter, as_player):
        for _ in range(2):
            assert self._rest(db_client, fighter, _session("p1"), "short").status_code == 200

    def test_dm_can_grant_another_long_rest(self, db_client, fighter, as_player):
        assert self._rest(db_client, fighter, _session("p1")).status_code == 200
        assert self._rest(db_client, fighter, _session("gm", role="dm")).status_code == 200


class TestDmRoleIsGranted:
    def _start(self, db_client, character: str, **body):
        return db_client.post(
            "/session/start", json={"character_id": character, "world_id": _WORLD, **body}, headers=_account("p1")
        )

    def test_solo_session_can_never_be_dm(self, db_client, fighter):
        resp = self._start(db_client, fighter, role="dm")
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_role"

    def test_unlisted_player_cannot_claim_dm(self, db_client, fighter, as_player):
        resp = self._start(db_client, fighter, role="dm", mode="multiplayer")
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"

    def test_listed_player_can_run_as_dm(self, db_client, fighter, as_player, monkeypatch):
        monkeypatch.setattr(_config.settings, "dm_player_ids", "someone_else, p1")
        resp = self._start(db_client, fighter, role="dm", mode="multiplayer")
        assert resp.status_code == 201, resp.text
        assert resp.json()["role"] == "dm"


class TestDiceNotation:
    """Item and ability files may use 'NdM+K'; the resolver used to crash on it."""

    @patch("relay.combat.resolver.random.randint", return_value=2)
    def test_flat_modifier_is_added(self, _roll) -> None:
        assert roll_dice("2d4+2") == (6, [2, 2])
        assert roll_dice("1d6-1") == (1, [2])

    def test_flat_number_and_bare_die(self) -> None:
        assert roll_dice("3") == (3, [3])
        total, rolls = roll_dice("d6")
        assert len(rolls) == 1 and 1 <= total <= 6

    @patch("relay.combat.resolver.random.randint", return_value=4)
    def test_weapon_bonus_counts_once_on_a_critical(self, _roll) -> None:
        weapon = {"stats": {"damage_dice": "1d6+2", "damage_type": "piercing", "modifier_source": "dexterity"}}
        result = damage_roll(weapon, {"dexterity": 16}, critical=True)
        assert result["rolls"] == [4, 4]
        assert result["raw_total"] == 4 + 4 + 2 + 3

    def test_damage_is_never_negative(self) -> None:
        weapon = {"stats": {"damage_dice": "1", "damage_type": "bludgeoning"}}
        assert damage_roll(weapon, {"strength": 4})["raw_total"] == 0
