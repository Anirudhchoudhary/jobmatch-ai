"""FastAPI app factory."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import ValidationError

from app import __version__
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.uploads import local_put_router
from app.api.uploads import router as uploads_router
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.request_id import RequestIdMiddleware
from app.core.settings import get_settings
from app.db.engine import get_engine


def _log_level() -> str:
    """Settings are optional at app creation (offline unit tests have no DATABASE_URL)."""
    try:
        return get_settings().log_level
    except ValidationError:
        return os.environ.get("LOG_LEVEL", "INFO")


def _env() -> str:
    try:
        return get_settings().env
    except ValidationError:
        return os.environ.get("ENV", "local")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    # Dispose only an engine that was actually created; never create one just to dispose it.
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


def create_app() -> FastAPI:
    # Runs at import time under uvicorn, after its dictConfig, so this override is intentional.
    configure_logging(_log_level())
    app = FastAPI(title="JobMatch AI", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(uploads_router)
    app.include_router(documents_router)
    if _env() in ("local", "ci"):
        app.include_router(local_put_router)
    return app


app = create_app()
