"""Server-sent-event helpers."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping

import structlog
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.errors import AppError, ProviderError
from app.core.request_id import request_id_var
from app.core.settings import get_settings

log = structlog.get_logger(__name__)


def sse_event(event: str, data: BaseModel | Mapping[str, object]) -> bytes:
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode()


def _error_event(code: str, message: str) -> bytes:
    return sse_event(
        "error", {"code": code, "message": message, "request_id": request_id_var.get()}
    )


_KEEPALIVE = b": keep-alive\n\n"


class _End:
    """Queue item: the source finished, with ``error`` set when it raised."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error


async def _pump(
    events: AsyncIterator[tuple[str, BaseModel]], queue: asyncio.Queue[bytes | _End]
) -> None:
    try:
        async for name, data in events:
            await queue.put(sse_event(name, data))
    except Exception as exc:  # noqa: BLE001 - handed to the consumer, which reports it
        await queue.put(_End(exc))
        return
    await queue.put(_End())


async def sse_stream(
    events: AsyncIterator[tuple[str, BaseModel]], *, keepalive_s: float = 15.0
) -> AsyncIterator[bytes]:
    """Encode events; failures become a final ``error`` event and are never re-raised.

    The source runs in its own task so a ``: keep-alive`` comment can be sent whenever nothing
    has been sent for ``keepalive_s`` (a slow model call would otherwise leave the connection
    silent long enough for CloudFront to drop it). Real events are never delayed.
    """
    queue: asyncio.Queue[bytes | _End] = asyncio.Queue(maxsize=1)
    producer = asyncio.create_task(_pump(events, queue))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
            except TimeoutError:
                if producer.done() and queue.empty():
                    # The source task ended without a final item (cancelled from inside).
                    yield _error_event("internal", "Something went wrong.")
                    return
                yield _KEEPALIVE
                continue
            if isinstance(item, _End):
                if item.error is not None:
                    yield _report(item.error)
                return
            yield item
    finally:
        # Client gone or stream finished: stop the source (closing its generator).
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer


def _report(exc: BaseException) -> bytes:
    if isinstance(exc, AppError):
        extra = {"provider": exc.provider} if isinstance(exc, ProviderError) else {}
        log.warning(
            "sse_app_error",
            code=exc.code,
            status=exc.status,
            request_id=request_id_var.get(),
            **extra,
        )
        return _error_event(exc.code, exc.message)
    log.error("sse_stream_error", request_id=request_id_var.get(), exc_info=exc)
    return _error_event("internal", "Something went wrong.")


def sse_response(
    events: AsyncIterator[tuple[str, BaseModel]], *, keepalive_s: float | None = None
) -> StreamingResponse:
    interval = get_settings().sse_keepalive_s if keepalive_s is None else keepalive_s
    return StreamingResponse(
        sse_stream(events, keepalive_s=interval),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
