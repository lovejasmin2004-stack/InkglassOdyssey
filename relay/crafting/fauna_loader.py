"""Fauna loader — resolves fauna data for gathering yields.

Fauna files live under ``fauna/{world_id}/{fauna_id}.json`` and describe
creatures that players can harvest gathering materials from.

Uses the shared :class:`ContentLoader` for async I/O, Pydantic validation,
and LRU-bounded caching.
"""

from __future__ import annotations

from relay.schemas import Fauna
from relay.world.base_loader import ContentLoader

_fauna_loader = ContentLoader(
    content_root="fauna",
    model=Fauna,
    max_cache_size=256,
)


async def load_fauna(fauna_id: str, world_id: str) -> Fauna | None:
    return await _fauna_loader.load(fauna_id, world_id)


async def invalidate_fauna(fauna_id: str, world_id: str) -> None:
    await _fauna_loader.invalidate(fauna_id, world_id)


async def reload_fauna(fauna_id: str, world_id: str) -> Fauna | None:
    return await _fauna_loader.reload(fauna_id, world_id)
