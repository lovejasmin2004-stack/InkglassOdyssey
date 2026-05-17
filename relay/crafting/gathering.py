"""Gathering logic — check-gated resource harvesting from regions.

Gathering is defined per region in the region content files. Each
gatherable node specifies:
- item_id: what item is yielded
- skill: the check skill (survival, nature, etc.)
- dc: difficulty

Yield range comes from world config (default 1-3 on success).
A failed check yields nothing. Materials are added directly to inventory.

Note: Gathering yield uses ``random.randint(yield_min, yield_max)`` rather than
the shared ``roll_d20()`` because the quantity is a uniform range roll, not a
d20 mechanic.  The skill check that gates gathering already uses roll_d20 via
the check resolver.
"""

from __future__ import annotations

import logging
import random

logger = logging.getLogger(__name__)

_DEFAULT_YIELD_MIN = 1
_DEFAULT_YIELD_MAX = 3


def resolve_gather_yield(
    node: dict,
    check_passed: bool,
    *,
    yield_min: int = _DEFAULT_YIELD_MIN,
    yield_max: int = _DEFAULT_YIELD_MAX,
) -> dict:
    """Determine gathering yield based on check result.

    Returns {item_id, quantity, success}.
    """
    item_id = node.get("item_id") or node.get("material_id", "")

    if not check_passed:
        logger.info(
            "Gather yield failed",
            extra={"item_id": item_id, "success": False},
        )
        return {
            "item_id": item_id,
            "material_id": item_id,
            "quantity": 0,
            "success": False,
        }

    actual_min = node.get("yield_min", yield_min)
    actual_max = node.get("yield_max", yield_max)
    quantity = random.randint(actual_min, actual_max)

    logger.info(
        "Gather yield resolved",
        extra={
            "item_id": item_id,
            "quantity": quantity,
            "yield_range": [actual_min, actual_max],
            "success": True,
        },
    )
    return {
        "item_id": item_id,
        "material_id": item_id,
        "quantity": quantity,
        "success": True,
    }


def add_gathered_to_inventory(
    material_id: str,
    quantity: int,
    inventory: list[dict],
) -> list[dict]:
    """Add gathered materials to inventory. Returns updated inventory."""
    if quantity <= 0:
        return inventory

    for entry in inventory:
        if entry.get("item_id") == material_id and entry.get("binding_state") == "unbound":
            entry["quantity"] = entry.get("quantity", 0) + quantity
            logger.info(
                "Gathered materials added to existing stack",
                extra={"material_id": material_id, "quantity": quantity},
            )
            return inventory

    inventory.append(
        {
            "item_id": material_id,
            "quantity": quantity,
            "binding_state": "unbound",
        }
    )
    logger.info(
        "Gathered materials added as new stack",
        extra={"material_id": material_id, "quantity": quantity},
    )
    return inventory
