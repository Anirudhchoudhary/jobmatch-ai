"""FastAPI app factory."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import ValidationError

from app import __version__
from app.api.analyses import router as analyses_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.samples import router as samples_router
from app.api.suggestions import router as suggestions_router
from app.api.uploads import local_put_router
from app.api.uploads import router as uploads_router
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.pii import warm_up
from app.core.request_id import RequestIdMiddleware
from app.core.settings import get_settings
from app.db.engine import get_engine
from app.observability.request_log import DbRequestLogWriter, RequestLogMiddleware


def _log_level() -> str:
    """Settings are optional at app creation (offline unit tests have no DATABASE_URL)."""
    try:
        return get_settings().log_level
    except ValidationError:
        return os.environ.get("LOG_LEVEL", "INFO")


def _pii_engine() -> Literal["regex", "presidio"]:
    try:
        return get_settings().pii_engine
    except ValidationError:
        return "regex"


def _env() -> str:
    try:
        return get_settings().env
    except ValidationError:
        return os.environ.get("ENV", "local")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if _pii_engine() == "presidio":
        await asyncio.to_thread(warm_up)  # first use would otherwise load spaCy mid-request
    yield
    # Dispose only an engine that was actually created; never create one just to dispose it.
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


def create_app() -> FastAPI:
    # Runs at import time under uvicorn, after its dictConfig, so this override is intentional.
    configure_logging(_log_level(), pii_engine=_pii_engine())
    app = FastAPI(title="JobMatch AI", version=__version__, lifespan=lifespan)
    # add_middleware stacks last-added outermost: the request id must be bound first.
    app.add_middleware(RequestLogMiddleware, writer=DbRequestLogWriter())
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(uploads_router)
    app.include_router(documents_router)
    app.include_router(chat_router)
    app.include_router(samples_router)
    app.include_router(analyses_router)
    app.include_router(suggestions_router)
    if _env() in ("local", "ci"):
        app.include_router(local_put_router)
    return app


app = create_app()
