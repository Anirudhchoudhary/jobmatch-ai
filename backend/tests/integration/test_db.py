from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.settings import get_settings
from app.db.engine import make_engine

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


async def test_can_connect(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1


async def test_pgvector_extension_after_migration(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        version = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        ).scalar_one()
        assert isinstance(version, str) and version
        distance = (
            await conn.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"))
        ).scalar_one()
        assert distance == 1.0
