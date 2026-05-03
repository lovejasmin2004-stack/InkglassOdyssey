from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from relay.database import get_db
from relay.main import app
from relay.models import Base

_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


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

    import asyncio
    asyncio.get_event_loop().run_until_complete(_create_tables())

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()

    asyncio.get_event_loop().run_until_complete(_drop_tables())
    asyncio.get_event_loop().run_until_complete(engine.dispose())
