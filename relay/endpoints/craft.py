"""Crafting and gathering endpoints."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
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
from relay.database import get_db
from relay.economy.shop import get_world_currency
from relay.economy.wallet import log_item_transaction
from relay.models import Character

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crafting"])

# TODO(Phase 1): Recipe discovery endpoint (POST /recipe/learn).  Currently
# recipes can only be added via PATCH /character.  In-game discovery should
# happen through NPC teaching, quest rewards, or loot — each feeding into a
# dedicated endpoint that validates the recipe exists and adds it to
# character.known_recipes.


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class RecipeMaterial(BaseModel):
    item_id: str
    quantity: int = Field(ge=1)


class RecipeInput(BaseModel):
    """Validated recipe payload — prevents KeyError 500s from malformed input."""

    id: str
    output_item_id: str
    output_quantity: int = Field(default=1, ge=1)
    input_materials: list[RecipeMaterial] = Field(min_length=1)
    required_skill: str = "survival"
    skill_dc: int = Field(default=15, ge=1, le=30)
    level_requirement: int = Field(default=1, ge=1, le=20)
    required_station_type: str | None = None
    name: str | None = None

    model_config = {"extra": "allow"}


class GatherNode(BaseModel):
    """Validated gather-node payload — required fields enforced, no silent defaults."""

    material_id: str
    skill: str
    dc: int = Field(ge=1, le=30)
    yield_min: int = Field(default=1, ge=1)
    yield_max: int = Field(default=1, ge=1)
    name: str | None = None

    model_config = {"extra": "allow"}


class CraftRequest(BaseModel):
    character_id: str
    recipe: RecipeInput
    station_type: str | None = None


class CraftResponse(BaseModel):
    success: bool
    check_result: dict
    output_item_id: str | None = None
    output_quantity: int = 0
    materials_consumed: bool = False
    materials_lost: list[dict] | None = None
    critical: bool = False
    error: str | None = None


class GatherRequest(BaseModel):
    character_id: str
    node: GatherNode


class GatherResponse(BaseModel):
    success: bool
    check_result: dict
    material_id: str
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
    """Attempt to craft an item from a recipe.

    Flow: validate requirements → skill check → consume materials → produce output.
    Failed check: 50% material loss. Failed validation: 400 error.
    Critical success (nat 20): bonus output quantity.
    """
    result = await db.execute(
        select(Character).where(
            Character.id == body.character_id,
            Character.player_id == token.player_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    recipe = body.recipe.model_dump(exclude_none=True)

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

    required_skill = recipe.get("required_skill", "survival")
    tool_advantage = has_tool_advantage(char.equipped_gear or {}, required_skill)

    check = validate_check(
        {
            "skill": required_skill,
            "dc": recipe.get("skill_dc", 15),
            "reason": f"Crafting: {recipe.get('name', recipe['id'])}",
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

        # (#6) Audit trail for failed crafts — records partial material loss
        lost_note = ", ".join(f"{m['quantity']}x {m['item_id']}" for m in lost)
        log_item_transaction(
            db,
            char,
            tx_type="craft_fail",
            item_id=recipe.get("output_item_id", ""),
            item_quantity=0,
            currency=get_world_currency(char.world_id),
            session_id=token.session_id,
            note=f"Failed craft {recipe.get('name', recipe['id'])}; lost: {lost_note}",
        )

        await db.commit()

        logger.info(
            "Craft failed check — partial material loss",
            extra={"recipe_id": recipe["id"], "character_id": char.id, "lost": lost},
        )
        return CraftResponse(
            success=False,
            check_result=check_result,
            output_item_id=recipe.get("output_item_id"),
            output_quantity=0,
            materials_consumed=False,
            materials_lost=lost,
            critical=False,
        )

    base_quantity = recipe.get("output_quantity", 1)
    output_quantity = base_quantity + 1 if is_critical else base_quantity

    inventory = list(char.inventory or [])
    inventory = consume_materials(recipe["input_materials"], inventory)
    inventory = produce_output(
        recipe["output_item_id"],
        output_quantity,
        inventory,
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
        note=f"Crafted {recipe.get('name', recipe['id'])}",
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
        materials_consumed=True,
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

    Flow: skill check against node DC → resolve yield → add to inventory.
    """
    result = await db.execute(
        select(Character).where(
            Character.id == body.character_id,
            Character.player_id == token.player_id,
        )
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(
            status_code=404,
            detail={"code": "character_not_found", "message": "Character not found"},
        )

    node = body.node.model_dump(exclude_none=True)

    check = validate_check(
        {
            "skill": node["skill"],
            "dc": node["dc"],
            "reason": f"Gathering: {node.get('name', node['material_id'])}",
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

    gather_result = resolve_gather_yield(node, check_result["passed"])

    if gather_result["success"]:
        inventory = list(char.inventory or [])
        inventory = add_gathered_to_inventory(
            gather_result["material_id"],
            gather_result["quantity"],
            inventory,
        )
        char.inventory = inventory
        flag_modified(char, "inventory")

        # (#11) Audit trail for gathered materials
        log_item_transaction(
            db,
            char,
            tx_type="gather",
            item_id=gather_result["material_id"],
            item_quantity=gather_result["quantity"],
            currency=get_world_currency(char.world_id),
            session_id=token.session_id,
            note=f"Gathered {node.get('name', gather_result['material_id'])}",
        )

        await db.commit()

        logger.info(
            "Gather succeeded",
            extra={
                "character_id": char.id,
                "material_id": gather_result["material_id"],
                "quantity": gather_result["quantity"],
            },
        )
    else:
        logger.info(
            "Gather failed check",
            extra={"character_id": char.id, "node_skill": node.get("skill")},
        )

    return GatherResponse(
        success=gather_result["success"],
        check_result=check_result,
        material_id=gather_result["material_id"],
        quantity=gather_result["quantity"],
    )
