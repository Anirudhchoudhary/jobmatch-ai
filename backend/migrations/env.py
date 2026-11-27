"""Alembic environment (async engine; URL comes from app settings)."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url.get_secret_value()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = make_engine(_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
