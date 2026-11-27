"""Per-request log: one ``request_log`` row per /api request, written by pure-ASGI middleware."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_id import request_id_var
from app.db.engine import get_sessionmaker
from app.db.models import RequestLog
from app.observability.usage import RequestUsage, usage_scope

log = structlog.get_logger(__name__)
_WRITE_TIMEOUT_S = 5


@dataclass(frozen=True)
class RequestLogEntry:
    request_id: str
    user_id: uuid.UUID | None
    route: str
    method: str
    status: int
    models: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    cache_hit: bool
    latency_ms: int
    guardrails: tuple[str, ...]


class RequestLogWriter(Protocol):
    async def write(self, entry: RequestLogEntry) -> None: ...


class DbRequestLogWriter:
    """Writes the row shielded from cancellation; failures are logged, never raised."""

    def __init__(
        self,
        sessionmaker_factory: Callable[[], async_sessionmaker[AsyncSession]] = get_sessionmaker,
    ) -> None:
        self._factory = sessionmaker_factory

    async def write(self, entry: RequestLogEntry) -> None:
        try:
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(_WRITE_TIMEOUT_S) as scope:
                    async with self._factory()() as session:
                        session.add(
                            RequestLog(
                                request_id=entry.request_id,
                                user_id=entry.user_id,
                                route=entry.route,
                                method=entry.method,
                                status=entry.status,
                                models=list(entry.models),
                                input_tokens=entry.input_tokens,
                                output_tokens=entry.output_tokens,
                                cache_read_tokens=entry.cache_read_tokens,
                                cache_write_tokens=entry.cache_write_tokens,
                                cost_usd=Decimal(str(entry.cost_usd)),
                                cache_hit=entry.cache_hit,
                                latency_ms=entry.latency_ms,
                                guardrails=list(entry.guardrails),
                            )
                        )
                        await session.commit()
                if scope.cancelled_caught:
                    log.warning("request_log_write_timeout", route=entry.route)
        except Exception as exc:
            log.warning("request_log_write_failed", error_type=type(exc).__name__)


def _entry(
    scope: Scope, usage: RequestUsage, status: int, latency_ms: int, request_id: str
) -> RequestLogEntry:
    return RequestLogEntry(
        request_id=request_id,
        user_id=usage.user_id,
        route=scope["path"],
        method=scope["method"],
        status=status,
        models=tuple(usage.models),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        cache_hit=usage.cache_hit,
        latency_ms=latency_ms,
        guardrails=tuple(usage.guardrails),
    )


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp, writer: RequestLogWriter) -> None:
        self.app = app
        self.writer = writer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        status = 0
        started = time.perf_counter()

        async def send_capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with usage_scope() as usage:
            try:
                await self.app(scope, receive, send_capture)
            finally:
                latency_ms = int((time.perf_counter() - started) * 1000)
                entry = _entry(scope, usage, status or 500, latency_ms, request_id_var.get())
                log.info(
                    "request_completed",
                    route=entry.route,
                    status=entry.status,
                    latency_ms=latency_ms,
                    cost_usd=entry.cost_usd,
                    cache_hit=entry.cache_hit,
                    model_count=len(entry.models),
                )
                with anyio.CancelScope(shield=True):
                    await self.writer.write(entry)
