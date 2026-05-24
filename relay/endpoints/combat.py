"""Combat endpoints — resolve attacks, start encounters, manage turn order."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.combat.conditions import (
    apply_condition,
    get_attack_modifiers,
    get_defense_modifiers,
    get_save_modifiers,
    tick_conditions,
)
from relay.combat.death_state import enter_death_state, heal_from_death_state, tick_death_state
from relay.combat.initiative import determine_turn_order
from relay.combat.resolver import (
    ENVIRONMENT_COMBAT_EFFECTS,
    apply_damage_resistances,
    attack_roll,
    compute_save_dc,
    damage_roll,
    resolve_attack,
    saving_throw,
)
from relay.database import get_db
from relay.endpoints._helpers import load_character_any, load_character_owned
from relay.mutations import (
    ConditionChange,
    DeathStateChange,
    ExhaustionChange,
    HPChange,
    RestEffect,
)
from relay.state_log import (
    log_condition_change,
    log_death_state,
    log_exhaustion_change,
    log_hp_change,
    log_rest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/combat", tags=["combat"])


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class AttackRequest(BaseModel):
    attacker_id: str
    target_id: str
    weapon: dict
    proficient: bool = True
    advantage: bool = False
    disadvantage: bool = False
    environmental_effects: list[str] = Field(default_factory=list)


class AttackResponse(BaseModel):
    hit: bool
    critical: bool
    auto_miss: bool
    attack_roll: dict
    damage: dict | None = None
    target_hp_after: int | None = None
    target_entered_death_state: bool = False


class SaveRequest(BaseModel):
    attacker_id: str
    defender_id: str
    save_type: str
    dc_source_ability: str
    damage_dice: str | None = None
    damage_type: str = "force"
    half_on_save: bool = True
    applies_condition: dict | None = None


class SaveResponse(BaseModel):
    passed: bool
    save_roll: dict
    damage: dict | None = None
    condition_applied: str | None = None
    defender_hp_after: int | None = None


class InitiativeRequest(BaseModel):
    participant_ids: list[str]


class InitiativeResponse(BaseModel):
    turn_order: list[dict]


class HealRequest(BaseModel):
    target_id: str
    healing: int = Field(ge=1)


class HealResponse(BaseModel):
    hp_after: int
    hp_max: int
    left_death_state: bool


class TickConditionsRequest(BaseModel):
    character_id: str
    current_turn: int


class RestRequest(BaseModel):
    character_id: str
    rest_type: str = Field(pattern=r"^(short|long)$")


class RestResponse(BaseModel):
    rest_type: str
    hp_before: int
    hp_after: int
    hp_max: int
    exhaustion_before: int
    exhaustion_after: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/attack", response_model=AttackResponse)
async def post_attack(
    body: AttackRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AttackResponse:
    """Resolve a single attack: roll, hit/miss, damage, apply to target."""
    attacker = await load_character_owned(db, body.attacker_id, token.player_id)
    target = await load_character_any(db, body.target_id)

    attacker_conditions = attacker.conditions or []
    target_conditions = target.conditions or []

    atk_mods = get_attack_modifiers(attacker_conditions, attacker.exhaustion_level)
    def_mods = get_defense_modifiers(target_conditions)

    has_advantage = body.advantage or def_mods["attackers_have_advantage"]
    has_disadvantage = body.disadvantage or atk_mods["attack_disadvantage"]

    # (#7) Ranged attacks against prone targets have disadvantage
    if def_mods.get("attackers_have_disadvantage"):
        has_disadvantage = True

    # (#8) Data-driven environmental effects
    for effect in body.environmental_effects:
        env = ENVIRONMENT_COMBAT_EFFECTS.get(effect, {})
        if env.get("attack") == "advantage":
            has_advantage = True
        elif env.get("attack") == "disadvantage":
            has_disadvantage = True

    atk = attack_roll(
        attacker.ability_scores,
        attacker.level,
        body.weapon,
        proficient=body.proficient,
        advantage=has_advantage,
        disadvantage=has_disadvantage,
    )

    target_ac = target.ac
    result = resolve_attack(atk, target_ac)

    if not result["hit"]:
        return AttackResponse(
            hit=False,
            critical=False,
            auto_miss=result.get("auto_miss", False),
            attack_roll=result,
            damage=None,
        )

    dmg = damage_roll(
        body.weapon,
        attacker.ability_scores,
        critical=result["critical"],
    )
    dmg = apply_damage_resistances(
        dmg,
        resistances=getattr(target, "resistances", None),
        vulnerabilities=getattr(target, "vulnerabilities", None),
        immunities=getattr(target, "immunities", None),
    )

    hp_before = target.hp_current
    new_hp = max(0, target.hp_current - dmg["final_damage"])
    target.hp_current = new_hp

    await log_hp_change(db, HPChange(
        character_id=target.id,
        value=-dmg["final_damage"],
        damage_type=dmg.get("damage_type"),
        hp_before=hp_before,
        hp_after=new_hp,
        hp_max=target.hp_max,
        source="combat_attack",
        source_id=attacker.id,
        reason=f"attack with {body.weapon.get('name', 'weapon')}",
    ))

    entered_death = False
    if new_hp == 0:
        ds = enter_death_state(0, target.exhaustion_level)
        if ds["in_death_state"]:
            exhaustion_before = target.exhaustion_level
            target.exhaustion_level = ds["exhaustion_level"]
            # (#6) Persist death state exhaustion tracking
            target.death_state_exhaustion_gained = ds.get("death_state_exhaustion_gained", 1)
            entered_death = True

            await log_death_state(db, DeathStateChange(
                character_id=target.id,
                entered=True,
                hp_before=hp_before,
                hp_after=0,
                exhaustion_level=target.exhaustion_level,
                source="combat_attack",
                reason=f"reduced to 0 HP by {attacker.id}",
            ))
            if target.exhaustion_level != exhaustion_before:
                await log_exhaustion_change(db, ExhaustionChange(
                    character_id=target.id,
                    old_level=exhaustion_before,
                    new_level=target.exhaustion_level,
                    source="death_state",
                    reason="entered death state",
                ))

    await db.commit()

    return AttackResponse(
        hit=True,
        critical=result["critical"],
        auto_miss=False,
        attack_roll=result,
        damage=dmg,
        target_hp_after=new_hp,
        target_entered_death_state=entered_death,
    )


@router.post("/save", response_model=SaveResponse)
async def post_save(
    body: SaveRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SaveResponse:
    """Force a saving throw on the defender."""
    attacker = await load_character_owned(db, body.attacker_id, token.player_id)
    defender = await load_character_any(db, body.defender_id)

    dc = compute_save_dc(attacker.ability_scores, attacker.level, body.dc_source_ability)

    defender_conditions = defender.conditions or []
    save_mods = get_save_modifiers(defender_conditions, defender.exhaustion_level, body.save_type)

    if save_mods["auto_fail"]:
        save_result = {
            "save_type": body.save_type,
            "dc": dc,
            "roll": 0,
            "dice": [0],
            "roll_mode": "auto_fail",
            "ability_modifier": 0,
            "proficiency_bonus": 0,
            "total": 0,
            "passed": False,
        }
    else:
        save_result = saving_throw(
            defender.ability_scores,
            defender.level,
            defender.saving_throw_proficiencies or [],
            body.save_type,
            dc,
            disadvantage=save_mods["save_disadvantage"],
        )

    dmg_result = None
    condition_applied = None
    defender_hp_before = defender.hp_current
    defender_hp_after = defender.hp_current

    if not save_result["passed"]:
        if body.damage_dice:
            from relay.combat.resolver import roll_dice

            total, rolls = roll_dice(body.damage_dice)
            dmg_result = {
                "damage_dice": body.damage_dice,
                "rolls": rolls,
                "raw_total": total,
                "damage_type": body.damage_type,
                "final_damage": total,
            }
            dmg_result = apply_damage_resistances(
                dmg_result,
                resistances=getattr(defender, "resistances", None),
                vulnerabilities=getattr(defender, "vulnerabilities", None),
                immunities=getattr(defender, "immunities", None),
            )
            defender.hp_current = max(0, defender.hp_current - dmg_result["final_damage"])
            defender_hp_after = defender.hp_current

            await log_hp_change(db, HPChange(
                character_id=defender.id,
                value=-dmg_result["final_damage"],
                damage_type=body.damage_type,
                hp_before=defender_hp_before,
                hp_after=defender_hp_after,
                hp_max=defender.hp_max,
                source="combat_save",
                source_id=attacker.id,
                reason=f"failed {body.save_type} save",
            ))

        if body.applies_condition:
            cid = body.applies_condition.get("condition_id", "")
            dur = body.applies_condition.get("duration", 3)
            conditions = list(defender.conditions or [])
            conditions = apply_condition(conditions, cid, duration_turns=dur, source=body.attacker_id)
            defender.conditions = conditions
            condition_applied = cid

            await log_condition_change(db, ConditionChange(
                character_id=defender.id,
                condition_id=cid,
                action="add",
                duration_turns=dur,
                source=body.attacker_id,
                reason=f"failed {body.save_type} save",
            ))
    elif body.half_on_save and body.damage_dice:
        from relay.combat.resolver import roll_dice

        total, rolls = roll_dice(body.damage_dice)
        halved = total // 2
        dmg_result = {
            "damage_dice": body.damage_dice,
            "rolls": rolls,
            "raw_total": halved,
            "damage_type": body.damage_type,
            "final_damage": halved,
        }
        dmg_result = apply_damage_resistances(
            dmg_result,
            resistances=getattr(defender, "resistances", None),
            vulnerabilities=getattr(defender, "vulnerabilities", None),
            immunities=getattr(defender, "immunities", None),
        )
        defender.hp_current = max(0, defender.hp_current - dmg_result["final_damage"])
        defender_hp_after = defender.hp_current

        await log_hp_change(db, HPChange(
            character_id=defender.id,
            value=-dmg_result["final_damage"],
            damage_type=body.damage_type,
            hp_before=defender_hp_before,
            hp_after=defender_hp_after,
            hp_max=defender.hp_max,
            source="combat_save_half",
            source_id=attacker.id,
            reason=f"passed {body.save_type} save (half damage)",
        ))

    await db.commit()

    return SaveResponse(
        passed=save_result["passed"],
        save_roll=save_result,
        damage=dmg_result,
        condition_applied=condition_applied,
        defender_hp_after=defender_hp_after,
    )


@router.post("/initiative", response_model=InitiativeResponse)
async def post_initiative(
    body: InitiativeRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InitiativeResponse:
    """Roll initiative for all participants and return turn order."""
    participants = []
    for pid in body.participant_ids:
        char = await load_character_any(db, pid)
        participants.append({"id": char.id, "ability_scores": char.ability_scores})

    turn_order = determine_turn_order(participants)
    return InitiativeResponse(turn_order=turn_order)


@router.post("/heal", response_model=HealResponse)
async def post_heal(
    body: HealRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HealResponse:
    """Apply healing to a character, potentially leaving death state."""
    target = await load_character_owned(db, body.target_id, token.player_id)

    hp_before = target.hp_current
    in_death_state = target.hp_current == 0

    if in_death_state:
        result = heal_from_death_state(target.hp_current, body.healing, target.exhaustion_level)
        target.hp_current = min(result["hp_current"], target.hp_max)
        left_death = not result["in_death_state"]
        if left_death:
            # (#6) Reset death state exhaustion counter on recovery
            target.death_state_exhaustion_gained = 0
            await log_death_state(db, DeathStateChange(
                character_id=target.id,
                entered=False,
                hp_before=hp_before,
                hp_after=target.hp_current,
                exhaustion_level=target.exhaustion_level,
                source="healing",
                reason="healed out of death state",
            ))
    else:
        target.hp_current = min(target.hp_current + body.healing, target.hp_max)
        left_death = False

    await log_hp_change(db, HPChange(
        character_id=target.id,
        value=body.healing,
        hp_before=hp_before,
        hp_after=target.hp_current,
        hp_max=target.hp_max,
        source="healing",
        reason="healing applied",
    ))

    await db.commit()

    return HealResponse(
        hp_after=target.hp_current,
        hp_max=target.hp_max,
        left_death_state=left_death,
    )


@router.post("/tick-conditions")
async def post_tick_conditions(
    body: TickConditionsRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Advance condition durations and death state for a character."""
    char = await load_character_owned(db, body.character_id, token.player_id)

    conditions = list(char.conditions or [])
    conditions = tick_conditions(conditions, body.current_turn)
    char.conditions = conditions

    death_state_result = None
    if char.hp_current == 0:
        # (#6) Use persisted death state tracking instead of transient attribute
        death_state_result = tick_death_state(0, char.exhaustion_level, char.death_state_exhaustion_gained)
        char.exhaustion_level = death_state_result["exhaustion_level"]
        char.death_state_exhaustion_gained = death_state_result["death_state_exhaustion_gained"]

    await db.commit()

    return {
        "character_id": char.id,
        "conditions": conditions,
        "exhaustion_level": char.exhaustion_level,
        "death_state": death_state_result,
    }


@router.post("/rest", response_model=RestResponse)
async def post_rest(
    body: RestRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestResponse:
    """Apply rest effects to a character (#10).

    Short rest: no immediate HP effect in Phase 0 (hit-dice spending deferred).
    Long rest: restore HP to max, reduce exhaustion by 1.
    Both clear death state tracking if no longer at 0 HP.
    """
    from relay.combat.conditions import reduce_exhaustion

    char = await load_character_owned(db, body.character_id, token.player_id)

    hp_before = char.hp_current
    exhaustion_before = char.exhaustion_level

    if body.rest_type == "long":
        char.hp_current = char.hp_max
        if char.exhaustion_level > 0:
            char.exhaustion_level = reduce_exhaustion(char.exhaustion_level)
        # Long rest ends death state (HP restored above 0)
        char.death_state_exhaustion_gained = 0
    # Short rest: Phase 0 — no mechanical effect beyond marking the rest.
    # Hit-dice spending will be added when resource tracking is implemented.

    await log_rest(db, RestEffect(
        character_id=char.id,
        rest_type=body.rest_type,
        hp_before=hp_before,
        hp_after=char.hp_current,
        exhaustion_before=exhaustion_before,
        exhaustion_after=char.exhaustion_level,
    ))
    if exhaustion_before != char.exhaustion_level:
        await log_exhaustion_change(db, ExhaustionChange(
            character_id=char.id,
            old_level=exhaustion_before,
            new_level=char.exhaustion_level,
            source="rest",
            reason=f"{body.rest_type} rest",
        ))

    await db.commit()

    return RestResponse(
        rest_type=body.rest_type,
        hp_before=hp_before,
        hp_after=char.hp_current,
        hp_max=char.hp_max,
        exhaustion_before=exhaustion_before,
        exhaustion_after=char.exhaustion_level,
    )
