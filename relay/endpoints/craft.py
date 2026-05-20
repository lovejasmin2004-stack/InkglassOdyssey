"""Crafting and gathering endpoints.

Invariant #1: Relay is source of truth. Recipes are loaded from disk, not
supplied by the client. Gather nodes are resolved from region files.

Invariant #8: LLM is never authoritative over mechanical state.
Invariant #14: All economy transactions through relay endpoints.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
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
from relay.crafting.fauna_loader import load_fauna
from relay.crafting.gathering import add_gathered_to_inventory, resolve_gather_yield
from relay.crafting.recipe_loader import load_recipe
from relay.crafting.region_loader import load_region
from relay.database import get_db
from relay.economy.shop import get_world_currency
from relay.economy.wallet import log_item_transaction
from relay.endpoints._helpers import load_character_owned
from relay.models import TransactionLog

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crafting"])

# ---------------------------------------------------------------------------
# Gather cooldown — DB time-window check (Fix #11)
# ---------------------------------------------------------------------------

_GATHER_COOLDOWN_SECONDS = 30


async def _check_gather_cooldown_db(
    db: AsyncSession,
    character_id: str,
    region_id: str,
    item_id: str,
) -> float:
    """Return seconds remaining on cooldown, or 0.0 if ready.

    Queries the transaction_log for the most recent successful gather of
    this item in this region by this character.  Survives relay restarts
    (unlike the old in-memory OrderedDict approach).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=_GATHER_COOLDOWN_SECONDS)
    # SQLite stores datetimes without timezone — strip tzinfo for comparison
    cutoff_naive = cutoff.replace(tzinfo=None)
    result = await db.execute(
        select(func.max(TransactionLog.created_at)).where(
            TransactionLog.character_id == character_id,
            TransactionLog.tx_type == "gather",
            TransactionLog.region_id == region_id,
            TransactionLog.item_id == item_id,
            TransactionLog.created_at >= cutoff_naive,
        )
    )
    last_gather = result.scalar_one_or_none()
    if last_gather is None:
        return 0.0

    # SQLite returns timezone-naive datetimes; normalise to UTC-aware
    if last_gather.tzinfo is None:
        last_gather = last_gather.replace(tzinfo=UTC)

    elapsed = (datetime.now(UTC) - last_gather).total_seconds()
    remaining = _GATHER_COOLDOWN_SECONDS - elapsed
    return max(0.0, remaining)


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
    node_item_id: str | None = Field(default=None, min_length=1)
    fauna_id: str | None = Field(default=None, min_length=1)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _exactly_one_source(self) -> GatherRequest:
        has_node = self.node_item_id is not None
        has_fauna = self.fauna_id is not None
        if has_node == has_fauna:
            raise ValueError("Exactly one of node_item_id or fauna_id must be provided")
        return self


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
        await log_item_transaction(
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
    from relay.world.item_loader import load_item

    output_binding = "unbound"
    output_item = await load_item(recipe["output_item_id"], char.world_id)
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

    await log_item_transaction(
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
    """Attempt to gather materials from a region node or fauna.

    Flow: validate player region → resolve source (node or fauna) →
    cooldown check → skill check (with environmental effects + tool
    advantage) → resolve yield → add to inventory → log transaction.

    Supports two gather sources (exactly one per request):
    - **node_item_id**: harvest a gathering node defined in the region file.
    - **fauna_id**: harvest materials from a fauna creature in the region.
    """
    char = await load_character_owned(db, body.character_id, token.player_id)

    # Fix #3: Region position validation — character must be in the region
    if char.current_region_id is not None and char.current_region_id != body.region_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "wrong_region",
                "message": (f"Character is in region '{char.current_region_id}', not '{body.region_id}'"),
            },
        )

    region = await load_region(body.region_id, char.world_id)
    if region is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "region_not_found", "message": f"Region '{body.region_id}' not found"},
        )

    # -----------------------------------------------------------------------
    # Resolve gather source: node or fauna (Fix #9)
    # -----------------------------------------------------------------------
    if body.fauna_id is not None:
        # Fauna gathering branch
        fauna = await load_fauna(body.fauna_id, char.world_id)
        if fauna is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "fauna_not_found",
                    "message": f"Fauna '{body.fauna_id}' not found",
                },
            )

        if body.region_id not in fauna.region_ids:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "fauna_not_in_region",
                    "message": f"Fauna '{body.fauna_id}' is not found in region '{body.region_id}'",
                },
            )

        if not fauna.gathering_yields:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "fauna_no_yields",
                    "message": f"Fauna '{body.fauna_id}' has no gathering yields",
                },
            )

        fauna_item_id = random.choice(fauna.gathering_yields)
        gather_skill = "survival"
        gather_dc = min(10 + fauna.level, 30)
        gather_reason = f"Fauna gathering: {body.fauna_id}"
        cooldown_item_id = body.fauna_id
        node = None
    else:
        # Node gathering branch (original path)
        if not region.gathering_nodes:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "no_gathering_nodes",
                    "message": f"Region '{body.region_id}' has no gathering nodes",
                },
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

        gather_skill = node.skill
        gather_dc = node.dc
        gather_reason = f"Gathering: {body.node_item_id}"
        cooldown_item_id = body.node_item_id  # type: ignore[assignment]
        fauna_item_id = None

    # -----------------------------------------------------------------------
    # Cooldown check — DB time-window query (Fix #11)
    # -----------------------------------------------------------------------
    cooldown_remaining = await _check_gather_cooldown_db(
        db,
        char.id,
        body.region_id,
        cooldown_item_id,
    )
    if cooldown_remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "gather_cooldown",
                "message": f"Gathering on cooldown ({cooldown_remaining:.0f}s remaining)",
            },
            # Fix #15: Retry-After header
            headers={"Retry-After": str(math.ceil(cooldown_remaining))},
        )

    # -----------------------------------------------------------------------
    # Skill check with environmental effects + tool advantage (Fix #5, #6)
    # -----------------------------------------------------------------------
    tool_advantage = has_tool_advantage(char.equipped_gear or {}, gather_skill)

    check = validate_check(
        {
            "skill": gather_skill,
            "dc": gather_dc,
            "reason": gather_reason,
            "advantage": tool_advantage,
        }
    )

    check_result = resolve_check(
        check,
        char.ability_scores,
        char.skill_proficiencies or [],
        char.level,
        conditions=char.conditions or [],
        environmental_effects=region.environmental_effects or [],
        exhaustion_level=char.exhaustion_level,
    )

    # -----------------------------------------------------------------------
    # Resolve yield (Fix #12, #14: pass GatheringNode directly)
    # -----------------------------------------------------------------------
    if body.fauna_id is not None:
        # Fauna: yield is always 1 of the randomly chosen item
        if check_result["passed"]:
            result_item_id = fauna_item_id  # type: ignore[assignment]
            result_quantity = 1
            result_success = True
        else:
            result_item_id = fauna_item_id  # type: ignore[assignment]
            result_quantity = 0
            result_success = False
    else:
        gather_result = resolve_gather_yield(node, check_result["passed"])  # type: ignore[arg-type]
        result_item_id = gather_result["item_id"]
        result_quantity = gather_result["quantity"]
        result_success = gather_result["success"]

    currency = get_world_currency(char.world_id)

    if result_success:
        # Fix #2: cooldown recorded only on success (transaction log serves as record)
        inventory = list(char.inventory or [])
        inventory = add_gathered_to_inventory(
            result_item_id,
            result_quantity,
            inventory,
        )
        char.inventory = inventory
        flag_modified(char, "inventory")

        await log_item_transaction(
            db,
            char,
            tx_type="gather",
            item_id=result_item_id,
            item_quantity=result_quantity,
            currency=currency,
            session_id=token.session_id,
            region_id=body.region_id,
            note=f"Gathered {cooldown_item_id} from {body.region_id}",
        )

        await db.commit()

        logger.info(
            "Gather succeeded",
            extra={
                "character_id": char.id,
                "item_id": result_item_id,
                "quantity": result_quantity,
            },
        )
    else:
        # Fix #10: log gather_fail transaction on failed check
        await log_item_transaction(
            db,
            char,
            tx_type="gather_fail",
            item_id=result_item_id,
            item_quantity=0,
            currency=currency,
            session_id=token.session_id,
            region_id=body.region_id,
            note=f"Failed to gather {cooldown_item_id} from {body.region_id}",
        )

        await db.commit()

        logger.info(
            "Gather failed check",
            extra={"character_id": char.id, "skill": gather_skill},
        )

    return GatherResponse(
        success=result_success,
        check_result=check_result,
        item_id=result_item_id,
        quantity=result_quantity,
    )
