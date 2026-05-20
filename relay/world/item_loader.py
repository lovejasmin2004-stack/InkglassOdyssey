"""Item loader — loads validated item definitions from disk via ContentLoader.

Items are authored as JSON in items/{world_id}/ and created/edited
through the admin interface.  The relay loads them server-side; clients
never supply item definitions directly (Invariant #1).
"""

from __future__ import annotations

from relay.schemas import Item
from relay.world.base_loader import ContentLoader

_item_loader = ContentLoader(
    content_root="items",
    model=Item,
    max_cache_size=512,
)


async def load_item(item_id: str, world_id: str) -> Item | None:
    return await _item_loader.load(item_id, world_id)


async def load_all_items(world_id: str) -> dict[str, Item]:
    return await _item_loader.load_all(world_id)


async def invalidate_item(item_id: str, world_id: str) -> None:
    await _item_loader.invalidate(item_id, world_id)


async def invalidate_item_world(world_id: str) -> None:
    await _item_loader.invalidate_world(world_id)


async def reload_item(item_id: str, world_id: str) -> Item | None:
    return await _item_loader.reload(item_id, world_id)
