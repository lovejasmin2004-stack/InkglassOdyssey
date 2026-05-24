"""Load and cache NPC generation templates.

Templates live at ``templates/{world_id}/npc_templates.json`` — an array of
NpcTemplate objects, one per role.  The loader reads them once and caches
by (world_id, role) for fast lookup.

Design doc: docs/design_proposals.md §1 (Three-Tier Content System)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_TEMPLATES_ROOT = Path(__file__).parents[2] / "templates"


# ---------------------------------------------------------------------------
# Pydantic model for template validation
# ---------------------------------------------------------------------------


class AbilityScoreProfile(BaseModel):
    """Which ability scores get high / medium / low values."""

    primary: list[str] = Field(min_length=1, max_length=2)
    secondary: list[str] = Field(default_factory=list)
    dump: list[str] = Field(default_factory=list)


class AnimationProfileDefaults(BaseModel):
    default_stance: str
    default_gaze: str
    emotional_state_to_animation: dict[str, str]


class FewShotTemplate(BaseModel):
    player_input: str
    npc_response_template: str
    context_tag: str


class ManipResistTemplate(BaseModel):
    player_input: str
    npc_refusal_template: str


class NpcTemplate(BaseModel):
    """Validated NPC generation template."""

    id: str
    world_id: str
    role_display: str
    entity_class: str
    level_range: list[int] = Field(min_length=2, max_length=2)
    hit_die: int
    ability_score_profile: AbilityScoreProfile
    ac_range: list[int] = Field(min_length=2, max_length=2)
    saving_throw_proficiencies: list[str] = Field(min_length=2, max_length=2)
    skill_proficiency_pool: list[str] = Field(min_length=2)
    skill_proficiency_count: int = Field(ge=1, le=6)
    animation_profile_defaults: AnimationProfileDefaults
    few_shot_templates: list[FewShotTemplate] = Field(min_length=2)
    manipulation_resistance_templates: list[ManipResistTemplate] = Field(min_length=1)
    name_pool: list[str] = Field(min_length=5)
    personality_trait_pool: list[str] = Field(min_length=3)
    faction_affinity: str | None = None
    consequence_profile: str = "protected"
    knowledge_scope: list[str] = Field(default_factory=list)
    ignorance_scope: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Cache: (world_id, role) → NpcTemplate
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, NpcTemplate]] = {}
_cache_lock = asyncio.Lock()


def _read_templates(path: Path) -> list[dict]:
    """Blocking I/O — run via to_thread."""
    return json.loads(path.read_text(encoding="utf-8"))


async def load_templates(world_id: str) -> dict[str, NpcTemplate]:
    """Load all NPC templates for a world, keyed by role ID.

    Returns an empty dict if no template file exists.

    Uses a double-check pattern: fast path under lock (cache hit),
    slow path does I/O outside the lock then re-checks before writing.
    """
    async with _cache_lock:
        if world_id in _cache:
            return _cache[world_id]

    path = _TEMPLATES_ROOT / world_id / "npc_templates.json"
    if not path.exists():
        logger.warning("No NPC templates found", extra={"world_id": world_id, "path": str(path)})
        return {}

    try:
        raw_list = await asyncio.to_thread(_read_templates, path)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Failed to read NPC templates",
            extra={"world_id": world_id, "error": str(exc)},
        )
        return {}

    templates: dict[str, NpcTemplate] = {}
    for raw in raw_list:
        try:
            tmpl = NpcTemplate.model_validate(raw)
            templates[tmpl.id] = tmpl
        except Exception:
            logger.exception(
                "Invalid NPC template, skipping",
                extra={"world_id": world_id, "template_id": raw.get("id", "?")},
            )

    async with _cache_lock:
        # Re-check: another coroutine may have populated the cache while
        # we were doing I/O outside the lock.
        if world_id in _cache:
            return _cache[world_id]
        _cache[world_id] = templates

    logger.info(
        "NPC templates loaded",
        extra={"world_id": world_id, "count": len(templates)},
    )
    return templates


async def get_template(world_id: str, role: str) -> NpcTemplate | None:
    """Get a specific NPC template by world and role ID."""
    templates = await load_templates(world_id)
    return templates.get(role)


def clear_cache() -> None:
    """Clear the template cache (for testing / hot-reload)."""
    _cache.clear()
