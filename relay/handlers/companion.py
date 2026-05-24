"""Handler for the Character.companions JSON column.

Encapsulates load/find/mutate/persist for companion state, eliminating
the repeated ``list(char.companions or [])`` → mutate → reassign →
``flag_modified`` boilerplate from endpoint code.

The handler accumulates mutations; call ``persist()`` once before
``db.commit()`` to write back to the ORM.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm.attributes import flag_modified

from relay.models import Character


class CompanionHandler:
    def __init__(self, character: Character) -> None:
        self._char = character
        self._companions: list[dict] = list(character.companions or [])
        self._dirty = False

    @property
    def all(self) -> list[dict]:
        return self._companions

    @property
    def active_count(self) -> int:
        return sum(1 for c in self._companions if c.get("active", True))

    def find(self, npc_id: str) -> dict | None:
        for c in self._companions:
            if c["npc_id"] == npc_id:
                return c
        return None

    def find_or_raise(self, npc_id: str) -> dict:
        comp = self.find(npc_id)
        if comp is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "companion_not_found",
                    "message": f"Companion {npc_id} not found",
                },
            )
        return comp

    def add(self, entry: dict) -> None:
        self._companions.append(entry)
        self._dirty = True

    def remove(self, npc_id: str) -> None:
        self._companions = [c for c in self._companions if c["npc_id"] != npc_id]
        self._dirty = True

    def mark_dirty(self) -> None:
        """Mark the handler as needing persistence after in-place mutations."""
        self._dirty = True

    def persist(self) -> None:
        """Write back to ORM column and flag for SQLAlchemy change detection."""
        self._char.companions = self._companions
        flag_modified(self._char, "companions")
