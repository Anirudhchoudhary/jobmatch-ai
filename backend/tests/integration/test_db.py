import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.settings import get_settings
from app.db.engine import make_engine

pytestmark = pytest.mark.integration

LOCAL_DB_URL = "postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch"
BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def migrated() -> Iterator[None]:
    """Run `alembic upgrade head` against the Compose DB (non-secret local default URL)."""
    previous = os.environ.get("DATABASE_URL")
    os.environ.setdefault("DATABASE_URL", LOCAL_DB_URL)
    get_settings.cache_clear()
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    yield
    get_settings.cache_clear()
    if previous is None:
        os.environ.pop("DATABASE_URL", None)


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
