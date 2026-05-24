"""Handler for the Character.inventory JSON column.

Encapsulates load/find/add/remove/persist for inventory state, eliminating
the repeated ``list(char.inventory or [])`` -> mutate -> reassign ->
``flag_modified`` boilerplate from endpoint and shop code.

Stacking rules: items stack by (item_id, binding_state). Selling and
removal prefer unbound stacks so bound items aren't accidentally consumed.
"""

from __future__ import annotations

from sqlalchemy.orm.attributes import flag_modified

from relay.models import Character


class InventoryHandler:
    def __init__(self, character: Character) -> None:
        self._char = character
        self._items: list[dict] = list(character.inventory or [])
        self._dirty = False

    @property
    def all(self) -> list[dict]:
        return self._items

    def find(self, item_id: str, *, prefer_unbound: bool = False) -> dict | None:
        """Find an inventory entry by item_id.

        When prefer_unbound is True (used for selling), returns an unbound
        stack first so selling isn't blocked by a bound stack appearing earlier.
        """
        fallback: dict | None = None
        for entry in self._items:
            if entry.get("item_id") == item_id:
                if not prefer_unbound:
                    return entry
                if entry.get("binding_state") != "bound":
                    return entry
                if fallback is None:
                    fallback = entry
        return fallback

    def add_item(
        self,
        item_id: str,
        quantity: int,
        *,
        binding_state: str = "unbound",
    ) -> None:
        """Add an item, stacking if an entry with matching binding_state exists."""
        for entry in self._items:
            if entry.get("item_id") == item_id and entry.get("binding_state") == binding_state:
                entry["quantity"] = entry.get("quantity", 1) + quantity
                self._dirty = True
                return

        self._items.append(
            {
                "item_id": item_id,
                "quantity": quantity,
                "binding_state": binding_state,
            }
        )
        self._dirty = True

    def remove_item(self, item_id: str, quantity: int) -> None:
        """Remove quantity of an item. Prefers unbound stacks."""
        target_idx: int | None = None
        for i, entry in enumerate(self._items):
            if entry.get("item_id") == item_id:
                if entry.get("binding_state") != "bound":
                    target_idx = i
                    break
                if target_idx is None:
                    target_idx = i

        if target_idx is not None:
            entry = self._items[target_idx]
            remaining = entry.get("quantity", 1) - quantity
            if remaining <= 0:
                self._items.pop(target_idx)
            else:
                entry["quantity"] = remaining
            self._dirty = True

    def replace(self, items: list[dict]) -> None:
        """Replace the entire inventory list (for bulk operations like consume_materials)."""
        self._items = items
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True

    def persist(self) -> None:
        """Write back to ORM column and flag for SQLAlchemy change detection."""
        self._char.inventory = self._items
        flag_modified(self._char, "inventory")
