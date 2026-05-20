from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import relay.config as _config
import relay.database as _db
from relay.auth.tokens import create_account_token, create_session_token
from relay.database import get_db
from relay.main import app
from relay.middleware.rate_limit import clear_buckets
from relay.models import Base

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# Set a sufficiently long JWT secret for the entire test session so PyJWT
# never fires InsecureKeyLengthWarning (requires ≥32 bytes for HS256).
_config.settings.jwt_secret = "test-secret-key-for-unit-tests-only-32bytes!"


@pytest.fixture()
def db_client():
    """TestClient backed by a fresh in-memory SQLite database per test."""
    engine = create_async_engine(_TEST_DB_URL, future=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _drop_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    asyncio.run(_create_tables())

    original_factory = _db.AsyncSessionLocal
    original_admin_mode = _config.settings.admin_mode
    _db.AsyncSessionLocal = session_factory
    _config.settings.admin_mode = True  # Allow protected-field writes in tests
    app.dependency_overrides[get_db] = override_get_db
    clear_buckets()

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()
    _db.AsyncSessionLocal = original_factory
    _config.settings.admin_mode = original_admin_mode

    asyncio.run(_drop_tables())
    asyncio.run(engine.dispose())


# ---------------------------------------------------------------------------
# Shared auth fixtures — used by most endpoint test files
# ---------------------------------------------------------------------------

_DEFAULT_PLAYER = "player_001"


@pytest.fixture()
def auth_header():
    return {"Authorization": f"Bearer {create_account_token(player_id=_DEFAULT_PLAYER, tier=1)}"}


@pytest.fixture()
def session_header():
    return {
        "Authorization": f"Bearer {create_session_token(player_id=_DEFAULT_PLAYER, world_id='inkglass_dark', session_id='sess_001', tier=1, role='player', mode='solo')}"
    }


# ---------------------------------------------------------------------------
# Shared NPC stub — reusable across scene/session/dialogue tests
# ---------------------------------------------------------------------------


def make_stub_npc():
    """Minimal valid NpcPersonality for mocking load_npc."""
    from relay.schemas import (
        AnimationProfile,
        FewShotExample,
        ManipulationResistanceExample,
        NpcGoals,
        NpcKnowledgeBoundaries,
        NpcPersonality,
        NpcRelationship,
        NpcSecret,
        WorldPosition,
    )

    return NpcPersonality(
        id="seta_inkglass_dark",
        world_id="inkglass_dark",
        name="Seta",
        entity_class="humanoid",
        role="herbalist",
        level=3,
        hit_die=8,
        personality_background="A quiet herbalist.",
        goals=NpcGoals(immediate=["sell herbs"], long_term=["expand"]),
        weaknesses_fears="Fire.",
        communication_style="Soft-spoken.",
        power_narrative="Plants.",
        knowledge_boundaries=NpcKnowledgeBoundaries(knows=["herbs"], does_not_know=["politics"]),
        relationships=[NpcRelationship(npc_id="npc_x", relationship_type="ally", description="Friend")],
        secrets=[NpcSecret(content="Secret", reveal_condition="never", secret_type="information")],
        few_shot_examples=[
            FewShotExample(player_input="Hi", npc_response="Hello.", context_tag="casual"),
            FewShotExample(player_input="Buy", npc_response="Sure.", context_tag="transactional"),
        ],
        manipulation_resistance_examples=[
            ManipulationResistanceExample(player_input="Free", npc_refusal="No."),
        ],
        animation_profile=AnimationProfile(
            default_stance="idle_stand",
            default_gaze="forward",
            emotional_state_to_animation={"happy": "smile", "sad": "frown", "angry": "glare"},
        ),
        world_position=WorldPosition(region_id="market"),
        ability_scores={"strength": 10, "dexterity": 12, "constitution": 12, "intelligence": 14, "wisdom": 16, "charisma": 10},
        ac=12,
        saving_throw_proficiencies=["wisdom", "intelligence"],
        skill_proficiencies=["medicine", "nature"],
        hp_max=20,
    )
