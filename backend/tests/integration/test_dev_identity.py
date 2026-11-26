from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import DEV_USER_ID, get_current_user_id
from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import User

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


async def _count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(User).where(User.id == DEV_USER_ID)
    return (await session.execute(stmt)).scalar_one()


async def test_returns_dev_user_and_creates_row(session: AsyncSession) -> None:
    assert await get_current_user_id(session) == DEV_USER_ID
    assert await _count(session) == 1


async def test_idempotent(session: AsyncSession) -> None:
    await get_current_user_id(session)
    assert await get_current_user_id(session) == DEV_USER_ID
    assert await _count(session) == 1


async def test_recreates_row_after_deletion(session: AsyncSession) -> None:
    await get_current_user_id(session)
    await session.execute(text("DELETE FROM users WHERE id = :i"), {"i": DEV_USER_ID})
    await session.commit()
    assert await _count(session) == 0
    assert await get_current_user_id(session) == DEV_USER_ID
    assert await _count(session) == 1
