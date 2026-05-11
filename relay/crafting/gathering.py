"""Gathering logic — check-gated resource harvesting from regions.

Gathering is defined per region. Each gatherable node specifies:
- material_id: what item is yielded
- skill: the check skill (survival, nature, etc.)
- dc: difficulty
- yield_min / yield_max: quantity range on success
- tier: common / uncommon / rare (informational, for balance validation)

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


def resolve_gather_yield(
    node: dict,
    check_passed: bool,
) -> dict:
    """Determine gathering yield based on check result.

    Returns {material_id, quantity} on success, or {material_id, quantity: 0} on failure.
    """
    if not check_passed:
        logger.info(
            "Gather yield failed",
            extra={"material_id": node["material_id"], "success": False},
        )
        return {
            "material_id": node["material_id"],
            "quantity": 0,
            "success": False,
        }

    yield_min = node.get("yield_min", 1)
    yield_max = node.get("yield_max", 1)
    quantity = random.randint(yield_min, yield_max)

    logger.info(
        "Gather yield resolved",
        extra={
            "material_id": node["material_id"],
            "quantity": quantity,
            "yield_range": [yield_min, yield_max],
            "success": True,
        },
    )
    return {
        "material_id": node["material_id"],
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
