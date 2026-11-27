"""Health endpoint: reports DB connectivity and pgvector availability."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.core.settings import get_settings
from app.db.engine import get_engine

log = structlog.get_logger(__name__)

router = APIRouter()

DbChecker = Callable[[], Awaitable["DbStatus"]]


class DbStatus(BaseModel):
    db: Literal["ok", "error"]
    pgvector: str  # version, "missing", or "unknown" when the db errored


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    db: Literal["ok", "error"]
    pgvector: str


async def check_database(engine: AsyncEngine, timeout_s: float) -> DbStatus:
    """Query pgvector's version, bounding total time (connect, pool wait, query) by timeout_s."""
    try:
        async with asyncio.timeout(timeout_s), engine.connect() as conn:
            result = await conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            version = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - any failure means degraded; log type only
        log.warning("health_db_check_failed", error_type=type(exc).__name__)
        return DbStatus(db="error", pgvector="unknown")
    return DbStatus(db="ok", pgvector=version if version is not None else "missing")


def get_db_checker() -> DbChecker:
    """Dependency: resolves the engine and settings lazily, so nothing connects at startup."""

    async def checker() -> DbStatus:
        try:
            settings = get_settings()
            engine = get_engine()
        except Exception as exc:  # noqa: BLE001 - config/secret failure means degraded
            log.warning("health_config_failed", error_type=type(exc).__name__)
            return DbStatus(db="error", pgvector="unknown")
        return await check_database(engine, settings.db_connect_timeout_s)

    return checker


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response, checker: Annotated[DbChecker, Depends(get_db_checker)]
) -> HealthResponse:
    status = await checker()
    healthy = status.db == "ok" and status.pgvector not in ("missing", "unknown")
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=__version__,
        db=status.db,
        pgvector=status.pgvector,
    )
