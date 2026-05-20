"""Generic async content file loader with caching and validation.

Provides a reusable base class that handles the common pattern shared by
all content loaders: async file I/O → JSON parse → Pydantic validation →
LRU-bounded cache with lock.

Usage:
    from relay.world.base_loader import ContentLoader
    from relay.schemas import Item

    _item_loader = ContentLoader(
        content_root=Path("items"),
        model=Item,
        max_cache_size=500,
    )

    item = await _item_loader.load("iron_sword", "inkglass_dark")
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parents[2]


class ContentLoader[T: BaseModel]:
    """Async content file loader with bounded cache.

    Type parameter T is the Pydantic model used for validation.
    Files that fail validation are logged and treated as missing.
    """

    def __init__(
        self,
        content_root: str | Path,
        model: type[T],
        *,
        max_cache_size: int = 512,
    ) -> None:
        self._root = _REPO_ROOT / content_root
        self._model = model
        self._max_cache_size = max_cache_size
        self._cache: OrderedDict[str, T] = OrderedDict()
        self._lock = asyncio.Lock()

    def _cache_key(self, content_id: str, world_id: str) -> str:
        return f"{world_id}:{content_id}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Blocking I/O — run via to_thread."""
        return json.loads(path.read_text(encoding="utf-8"))

    def _evict_if_needed(self) -> None:
        """Evict oldest entries if cache exceeds max size. Call under lock."""
        while len(self._cache) > self._max_cache_size:
            self._cache.popitem(last=False)

    async def load(self, content_id: str, world_id: str) -> T | None:
        """Load content by ID and world. Returns None if not found."""
        key = self._cache_key(content_id, world_id)
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

        path = self._root / world_id / f"{content_id}.json"
        if not path.exists():
            return None

        try:
            data = await asyncio.to_thread(self._read_json, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read content file",
                extra={"path": str(path), "error": str(exc)},
            )
            return None

        try:
            obj = self._model.model_validate(data)
        except ValidationError as exc:
            logger.warning(
                "Content file failed validation",
                extra={"path": str(path), "model": self._model.__name__, "error": str(exc)},
            )
            return None

        async with self._lock:
            self._cache[key] = obj
            self._evict_if_needed()
        return obj

    async def load_all(self, world_id: str) -> dict[str, T]:
        """Load all content files for a world. Returns dict keyed by file stem."""
        world_dir = self._root / world_id
        if not world_dir.is_dir():
            return {}

        results: dict[str, T] = {}
        for path in world_dir.glob("*.json"):
            content_id = path.stem
            obj = await self.load(content_id, world_id)
            if obj is not None:
                results[content_id] = obj
        return results

    async def invalidate(self, content_id: str, world_id: str) -> None:
        """Remove a specific entry from cache."""
        key = self._cache_key(content_id, world_id)
        async with self._lock:
            self._cache.pop(key, None)

    async def invalidate_world(self, world_id: str) -> None:
        """Remove all cached entries for a world."""
        prefix = f"{world_id}:"
        async with self._lock:
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                del self._cache[k]

    async def reload(self, content_id: str, world_id: str) -> T | None:
        """Invalidate then re-load from disk."""
        await self.invalidate(content_id, world_id)
        return await self.load(content_id, world_id)

    def clear_cache(self) -> None:
        """Clear entire cache (for testing or shutdown)."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        return len(self._cache)
