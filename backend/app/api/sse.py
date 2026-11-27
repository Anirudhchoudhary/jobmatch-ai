"""Server-sent-event helpers."""

import json
from collections.abc import AsyncIterator, Mapping

import structlog
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.errors import AppError, ProviderError
from app.core.request_id import request_id_var

log = structlog.get_logger(__name__)


def sse_event(event: str, data: BaseModel | Mapping[str, object]) -> bytes:
    payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else dict(data)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode()


def _error_event(code: str, message: str) -> bytes:
    return sse_event(
        "error", {"code": code, "message": message, "request_id": request_id_var.get()}
    )


async def sse_stream(events: AsyncIterator[tuple[str, BaseModel]]) -> AsyncIterator[bytes]:
    """Encode events; failures become a final ``error`` event and are never re-raised."""
    try:
        async for name, data in events:
            yield sse_event(name, data)
    except AppError as exc:
        extra = {"provider": exc.provider} if isinstance(exc, ProviderError) else {}
        log.warning(
            "sse_app_error",
            code=exc.code,
            status=exc.status,
            request_id=request_id_var.get(),
            **extra,
        )
        yield _error_event(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        log.error("sse_stream_error", request_id=request_id_var.get(), exc_info=exc)
        yield _error_event("internal", "Something went wrong.")


def sse_response(events: AsyncIterator[tuple[str, BaseModel]]) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
