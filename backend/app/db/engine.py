"""Async SQLAlchemy engine factory."""

from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import get_settings


def make_engine(url: str, *, connect_timeout_s: float = 3.0) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(1, round(connect_timeout_s))},
    )


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return make_engine(
        settings.database_url.get_secret_value(),
        connect_timeout_s=settings.db_connect_timeout_s,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
