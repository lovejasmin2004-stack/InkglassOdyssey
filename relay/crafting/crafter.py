"""Crafting logic — recipe validation, material consumption, item production.

Crafting is check-gated: the player must pass a skill check at the recipe's DC.
On success, input materials are consumed and the output item is added to inventory.
On failure, 50% of materials are consumed (rounded up per material).

Mutation conventions
--------------------
- **New-list** (caller must use return value): ``consume_materials``,
  ``consume_partial_materials``, ``check_materials``.
- **In-place** (mutates the passed list; returns it for chaining): ``produce_output``.

All callers should reassign: ``inventory = consume_materials(...)``.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class RecipeNotKnownError(Exception):
    def __init__(self, recipe_id: str) -> None:
        self.recipe_id = recipe_id
        super().__init__(f"Recipe {recipe_id} not known by character")


class MissingMaterialsError(Exception):
    def __init__(self, missing: list[dict]) -> None:
        self.missing = missing
        super().__init__(f"Missing materials: {missing}")


class LevelTooLowError(Exception):
    def __init__(self, required: int, current: int) -> None:
        self.required = required
        self.current = current
        super().__init__(f"Level {current} too low, need {required}")


class StationRequiredError(Exception):
    def __init__(self, required: str, available: str | None) -> None:
        self.required = required
        self.available = available
        super().__init__(f"Requires station '{required}', have '{available}'")


def validate_recipe_requirements(
    recipe: dict,
    character_level: int,
    known_recipes: list[str],
    inventory: list[dict],
    station_type: str | None = None,
) -> None:
    """Validate that a character can attempt this recipe.

    Raises an appropriate exception if requirements are not met.
    """
    if recipe["id"] not in known_recipes:
        raise RecipeNotKnownError(recipe["id"])

    if character_level < recipe.get("level_requirement", 1):
        raise LevelTooLowError(recipe["level_requirement"], character_level)

    required_station = recipe.get("required_station_type")
    if required_station and station_type != required_station:
        raise StationRequiredError(required_station, station_type)

    missing = check_materials(recipe["input_materials"], inventory)
    if missing:
        raise MissingMaterialsError(missing)


def check_materials(
    input_materials: list[dict],
    inventory: list[dict],
) -> list[dict]:
    """Check if inventory contains all required materials. Returns missing items."""
    inv_map: dict[str, int] = {}
    for entry in inventory:
        item_id = entry.get("item_id", "")
        inv_map[item_id] = inv_map.get(item_id, 0) + entry.get("quantity", 0)

    missing = []
    for mat in input_materials:
        item_id = mat["item_id"]
        needed = mat["quantity"]
        have = inv_map.get(item_id, 0)
        if have < needed:
            missing.append({"item_id": item_id, "needed": needed, "have": have})

    return missing


def consume_materials(
    input_materials: list[dict],
    inventory: list[dict],
) -> list[dict]:
    """Remove crafting materials from inventory. Returns updated inventory."""
    consumption: dict[str, int] = {}
    for mat in input_materials:
        consumption[mat["item_id"]] = consumption.get(mat["item_id"], 0) + mat["quantity"]

    updated = []
    for entry in inventory:
        item_id = entry.get("item_id", "")
        if item_id in consumption:
            remaining_to_consume = consumption[item_id]
            current_qty = entry.get("quantity", 0)
            if remaining_to_consume >= current_qty:
                consumption[item_id] -= current_qty
                continue
            else:
                entry = {**entry, "quantity": current_qty - remaining_to_consume}
                consumption[item_id] = 0
        updated.append(entry)

    logger.info("Materials consumed", extra={"materials": input_materials})
    return updated


def consume_partial_materials(
    input_materials: list[dict],
    inventory: list[dict],
    loss_fraction: float = 0.5,
) -> tuple[list[dict], list[dict]]:
    """Consume a fraction of materials on a failed craft (default 50%, rounded up).

    Returns (updated_inventory, lost_materials) where lost_materials lists what was consumed.
    """
    partial: list[dict] = []
    for mat in input_materials:
        lost_qty = math.ceil(mat["quantity"] * loss_fraction)
        partial.append({"item_id": mat["item_id"], "quantity": lost_qty})

    updated_inventory = consume_materials(partial, inventory)
    logger.info(
        "Partial materials consumed on failed craft",
        extra={"loss_fraction": loss_fraction, "lost": partial},
    )
    return updated_inventory, partial


def has_tool_advantage(
    equipped_gear: dict,
    required_skill: str,
) -> bool:
    """Check if equipped gear grants advantage on a craft check.

    A tool grants advantage if its associated_skill matches the recipe's required_skill.

    Note: currently accepts tools in *any* gear slot.  When equip endpoints
    are implemented (Phase 2), consider restricting to ``tool_slot`` /
    ``off_hand`` so that a weapon with ``item_type: "tool"`` in main_hand
    doesn't accidentally grant crafting advantage.
    """
    for _slot, item in equipped_gear.items():
        if not isinstance(item, dict):
            continue
        if item.get("item_type") == "tool" and item.get("associated_skill") == required_skill:
            return True
    return False


def produce_output(
    output_item_id: str,
    output_quantity: int,
    inventory: list[dict],
    binding: str = "unbound",
) -> list[dict]:
    """Add crafted item to inventory (in-place mutation). Returns the same list."""
    for entry in inventory:
        if entry.get("item_id") == output_item_id and entry.get("binding_state") == binding:
            entry["quantity"] = entry.get("quantity", 0) + output_quantity
            logger.info(
                "Crafted output stacked onto existing entry",
                extra={"item_id": output_item_id, "quantity": output_quantity},
            )
            return inventory

    inventory.append(
        {
            "item_id": output_item_id,
            "quantity": output_quantity,
            "binding_state": binding,
        }
    )
    logger.info(
        "Crafted output added as new entry",
        extra={"item_id": output_item_id, "quantity": output_quantity},
    )
    return inventory
