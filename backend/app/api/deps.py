"""Request dependencies: DB session, clock and the signed-in user."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import user_from_token
from app.core.errors import AppError
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.db.models import User
from app.observability.usage import current_usage

DEV_USER_ID = UUID("00000000-0000-4000-8000-000000000001")


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def get_now() -> datetime:
    """The clock; tests override this dependency instead of sleeping."""
    return datetime.now(UTC)


def get_session_token(request: Request) -> str | None:
    return request.cookies.get(get_settings().session_cookie_name)


async def get_current_user_id(
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str | None, Depends(get_session_token)] = None,
    now: Annotated[datetime | None, Depends(get_now)] = None,
) -> UUID:
    """The signed-in user; local/ci fall back to the dev user when there is no valid session."""
    settings = get_settings()
    user = await user_from_token(session, token, settings=settings, now=now or get_now())
    if user is not None:
        if (usage := current_usage()) is not None:
            usage.user_id = user.id
        return user.id
    if settings.env not in ("local", "ci"):
        raise AppError("auth_required", "Sign in to continue.", 401)
    await session.execute(
        insert(User).values(id=DEV_USER_ID).on_conflict_do_nothing(index_elements=["id"])
    )
    await session.commit()
    if (usage := current_usage()) is not None:
        usage.user_id = DEV_USER_ID
    return DEV_USER_ID
