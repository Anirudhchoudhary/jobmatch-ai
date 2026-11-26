"""Request dependencies: DB session and the (dev-only) identity seam."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.db.models import User

DEV_USER_ID = UUID("00000000-0000-4000-8000-000000000001")

_dev_user_ready = False


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def get_current_user_id(session: Annotated[AsyncSession, Depends(get_session)]) -> UUID:
    """Dev identity; the auth phase replaces this function, routes keep its signature."""
    global _dev_user_ready  # noqa: PLW0603
    if get_settings().env not in ("local", "ci"):
        raise AppError("auth_required", "Sign in to continue.", 401)
    if not _dev_user_ready:
        await session.execute(
            insert(User).values(id=DEV_USER_ID).on_conflict_do_nothing(index_elements=["id"])
        )
        await session.commit()
        _dev_user_ready = True
    return DEV_USER_ID
