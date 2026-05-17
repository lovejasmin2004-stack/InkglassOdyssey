"""Region loader for gathering — resolves gather nodes from region files.

The relay loads region data server-side; clients send only a region_id
and node identifier.  This enforces Invariant #1 (relay is source of truth)
for DCs, skills, and yields.
"""

from __future__ import annotations

from relay.schemas import Region
from relay.world.base_loader import ContentLoader

_region_loader = ContentLoader(
    content_root="regions",
    model=Region,
    max_cache_size=128,
)


async def load_region(region_id: str, world_id: str) -> Region | None:
    return await _region_loader.load(region_id, world_id)


async def invalidate_region(region_id: str, world_id: str) -> None:
    await _region_loader.invalidate(region_id, world_id)


async def reload_region(region_id: str, world_id: str) -> Region | None:
    return await _region_loader.reload(region_id, world_id)
