"""Recipe loader — loads validated recipes from disk via ContentLoader.

Recipes are authored as JSON in crafting/{world_id}/ and created/edited
through the admin interface.  The relay loads them server-side; clients
never supply recipe definitions directly (Invariant #1).
"""

from __future__ import annotations

from relay.schemas import Recipe
from relay.world.base_loader import ContentLoader

_recipe_loader = ContentLoader(
    content_root="crafting",
    model=Recipe,
    max_cache_size=256,
)


async def load_recipe(recipe_id: str, world_id: str) -> Recipe | None:
    return await _recipe_loader.load(recipe_id, world_id)


async def load_all_recipes(world_id: str) -> dict[str, Recipe]:
    return await _recipe_loader.load_all(world_id)


async def invalidate_recipe(recipe_id: str, world_id: str) -> None:
    await _recipe_loader.invalidate(recipe_id, world_id)


async def invalidate_recipe_world(world_id: str) -> None:
    await _recipe_loader.invalidate_world(world_id)


async def reload_recipe(recipe_id: str, world_id: str) -> Recipe | None:
    return await _recipe_loader.reload(recipe_id, world_id)
