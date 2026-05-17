"""Crafting and gathering endpoints.

Invariant #1: Relay is source of truth. Recipes are loaded from disk, not
supplied by the client. Gather nodes are resolved from region files.

Invariant #8: LLM is never authoritative over mechanical state.
Invariant #14: All economy transactions through relay endpoints.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from relay.auth.middleware import require_session_token
from relay.auth.tokens import SessionTokenPayload
from relay.checks.resolver import resolve_check, validate_check
from relay.crafting.crafter import (
    LevelTooLowError,
    MissingMaterialsError,
    RecipeNotKnownError,
    StationRequiredError,
    consume_materials,
    consume_partial_materials,
    has_tool_advantage,
    produce_output,
    validate_recipe_requirements,
)
from relay.crafting.gathering import add_gathered_to_inventory, resolve_gather_yield
from relay.crafting.recipe_loader import load_recipe
from relay.crafting.region_loader import load_region
from relay.database import get_db
from relay.economy.shop import get_world_currency
from relay.economy.wallet import log_item_transaction
from relay.endpoints._helpers import load_character_owned

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crafting"])

# ---------------------------------------------------------------------------
# Gather cooldown tracking (in-memory, per-process)
# ---------------------------------------------------------------------------

_GATHER_COOLDOWN_SECONDS = 30
_MAX_COOLDOWN_ENTRIES = 4096
_gather_cooldowns: OrderedDict[str, float] = OrderedDict()


def _cooldown_key(character_id: str, region_id: str, item_id: str) -> str:
    return f"{character_id}:{region_id}:{item_id}"


def _check_gather_cooldown(character_id: str, region_id: str, item_id: str) -> float:
    """Return seconds remaining on cooldown, or 0 if ready."""
    key = _cooldown_key(character_id, region_id, item_id)
    last_time = _gather_cooldowns.get(key)
    if last_time is None:
        return 0.0
    elapsed = time.monotonic() - last_time
    remaining = _GATHER_COOLDOWN_SECONDS - elapsed
    return max(0.0, remaining)


def _record_gather(character_id: str, region_id: str, item_id: str) -> None:
    """Record a gather attempt timestamp."""
    key = _cooldown_key(character_id, region_id, item_id)
    _gather_cooldowns[key] = time.monotonic()
    _gather_cooldowns.move_to_end(key)
    while len(_gather_cooldowns) > _MAX_COOLDOWN_ENTRIES:
        _gather_cooldowns.popitem(last=False)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class CraftRequest(BaseModel):
    character_id: str = Field(min_length=1)
    recipe_id: str = Field(min_length=1)
    station_type: str | None = None

    model_config = {"extra": "forbid"}


class CraftResponse(BaseModel):
    success: bool
    check_result: dict
    output_item_id: str | None = None
    output_quantity: int = 0
    materials_consumed: list[dict] | None = None
    materials_lost: list[dict] | None = None
    critical: bool = False
    error: str | None = None


class GatherRequest(BaseModel):
    character_id: str = Field(min_length=1)
    region_id: str = Field(min_length=1)
    node_item_id: str = Field(min_length=1)

    model_config = {"extra": "forbid"}


class GatherResponse(BaseModel):
    success: bool
    check_result: dict
    item_id: str
    quantity: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/craft", response_model=CraftResponse)
async def post_craft(
    body: CraftRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CraftResponse:
    """Attempt to craft an item from a known recipe.

    Flow: load recipe from disk → validate requirements → skill check →
    consume materials → produce output.

    Failed check: 50% material loss. Failed validation: 400 error.
    Critical success (nat 20): bonus output quantity.
    """
    char = await load_character_owned(db, body.character_id, token.player_id)

    recipe_obj = await load_recipe(body.recipe_id, char.world_id)
    if recipe_obj is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "recipe_not_found", "message": f"Recipe '{body.recipe_id}' not found"},
        )

    recipe = recipe_obj.model_dump()

    try:
        validate_recipe_requirements(
            recipe,
            char.level,
            char.known_recipes or [],
            char.inventory or [],
            station_type=body.station_type,
        )
    except RecipeNotKnownError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "recipe_not_known", "message": str(e)},
        ) from None
    except LevelTooLowError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "level_too_low", "message": str(e)},
        ) from None
    except MissingMaterialsError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_materials", "message": str(e)},
        ) from None
    except StationRequiredError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "station_required", "message": str(e)},
        ) from None

    required_skill = recipe["required_skill"]
    tool_advantage = has_tool_advantage(char.equipped_gear or {}, required_skill)

    check = validate_check(
        {
            "skill": required_skill,
            "dc": recipe["skill_dc"],
            "reason": f"Crafting: {recipe.get('name') or recipe['id']}",
            "advantage": tool_advantage,
        }
    )

    check_result = resolve_check(
        check,
        char.ability_scores,
        char.skill_proficiencies or [],
        char.level,
        conditions=char.conditions or [],
        exhaustion_level=char.exhaustion_level,
    )

    is_critical = check_result["roll"] == 20

    if not check_result["passed"]:
        inventory = list(char.inventory or [])
        inventory, lost = consume_partial_materials(recipe["input_materials"], inventory)
        char.inventory = inventory
        flag_modified(char, "inventory")

        lost_note = ", ".join(f"{m['quantity']}x {m['item_id']}" for m in lost)
        log_item_transaction(
            db,
            char,
            tx_type="craft_fail",
            item_id=recipe["output_item_id"],
            item_quantity=0,
            currency=get_world_currency(char.world_id),
            session_id=token.session_id,
            note=f"Failed craft {recipe.get('name') or recipe['id']}; lost: {lost_note}",
        )

        await db.commit()

        logger.info(
            "Craft failed check — partial material loss",
            extra={"recipe_id": recipe["id"], "character_id": char.id, "lost": lost},
        )
        return CraftResponse(
            success=False,
            check_result=check_result,
            output_item_id=recipe["output_item_id"],
            output_quantity=0,
            materials_consumed=None,
            materials_lost=lost,
            critical=False,
        )

    base_quantity = recipe.get("output_quantity", 1)
    output_quantity = base_quantity + 1 if is_critical else base_quantity

    # Determine output binding from item definition
    from relay.endpoints.shop import _load_item

    output_binding = "unbound"
    output_item = await _load_item(recipe["output_item_id"], char.world_id)
    if output_item and output_item.binding == "bind_on_acquire":
        output_binding = "bound"

    inventory = list(char.inventory or [])
    consumed_materials = recipe["input_materials"]
    inventory = consume_materials(consumed_materials, inventory)
    inventory = produce_output(
        recipe["output_item_id"],
        output_quantity,
        inventory,
        binding=output_binding,
    )
    char.inventory = inventory
    flag_modified(char, "inventory")

    log_item_transaction(
        db,
        char,
        tx_type="craft",
        item_id=recipe["output_item_id"],
        item_quantity=output_quantity,
        currency=get_world_currency(char.world_id),
        session_id=token.session_id,
        note=f"Crafted {recipe.get('name') or recipe['id']}",
    )

    await db.commit()

    logger.info(
        "Craft succeeded",
        extra={
            "recipe_id": recipe["id"],
            "character_id": char.id,
            "output": recipe["output_item_id"],
            "quantity": output_quantity,
            "critical": is_critical,
        },
    )

    return CraftResponse(
        success=True,
        check_result=check_result,
        output_item_id=recipe["output_item_id"],
        output_quantity=output_quantity,
        materials_consumed=consumed_materials,
        materials_lost=None,
        critical=is_critical,
    )


@router.post("/gather", response_model=GatherResponse)
async def post_gather(
    body: GatherRequest,
    token: Annotated[SessionTokenPayload, Depends(require_session_token)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GatherResponse:
    """Attempt to gather materials from a region node.

    Flow: validate player region → load node from region file →
    cooldown check → skill check → resolve yield → add to inventory.
    """
    char = await load_character_owned(db, body.character_id, token.player_id)

    region = await load_region(body.region_id, char.world_id)
    if region is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "region_not_found", "message": f"Region '{body.region_id}' not found"},
        )

    if not region.gathering_nodes:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_gathering_nodes", "message": f"Region '{body.region_id}' has no gathering nodes"},
        )

    node = None
    for n in region.gathering_nodes:
        if n.item_id == body.node_item_id:
            node = n
            break

    if node is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "node_not_found",
                "message": f"No gathering node for '{body.node_item_id}' in region '{body.region_id}'",
            },
        )

    cooldown_remaining = _check_gather_cooldown(char.id, body.region_id, body.node_item_id)
    if cooldown_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "gather_cooldown",
                "message": f"Gathering on cooldown ({cooldown_remaining:.0f}s remaining)",
            },
        )

    node_dict = node.model_dump()

    check = validate_check(
        {
            "skill": node.skill,
            "dc": node.dc,
            "reason": f"Gathering: {body.node_item_id}",
        }
    )

    check_result = resolve_check(
        check,
        char.ability_scores,
        char.skill_proficiencies or [],
        char.level,
        conditions=char.conditions or [],
        exhaustion_level=char.exhaustion_level,
    )

    gather_result = resolve_gather_yield(node_dict, check_result["passed"])

    _record_gather(char.id, body.region_id, body.node_item_id)

    if gather_result["success"]:
        inventory = list(char.inventory or [])
        inventory = add_gathered_to_inventory(
            gather_result["item_id"],
            gather_result["quantity"],
            inventory,
        )
        char.inventory = inventory
        flag_modified(char, "inventory")

        log_item_transaction(
            db,
            char,
            tx_type="gather",
            item_id=gather_result["item_id"],
            item_quantity=gather_result["quantity"],
            currency=get_world_currency(char.world_id),
            session_id=token.session_id,
            note=f"Gathered {body.node_item_id} from {body.region_id}",
        )

        await db.commit()

        logger.info(
            "Gather succeeded",
            extra={
                "character_id": char.id,
                "item_id": gather_result["item_id"],
                "quantity": gather_result["quantity"],
            },
        )
    else:
        logger.info(
            "Gather failed check",
            extra={"character_id": char.id, "node_skill": node.skill},
        )

    return GatherResponse(
        success=gather_result["success"],
        check_result=check_result,
        item_id=gather_result["item_id"],
        quantity=gather_result["quantity"],
    )
