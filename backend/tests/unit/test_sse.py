import json
from collections.abc import AsyncIterator

from pydantic import BaseModel

from app.api.sse import sse_event, sse_stream
from app.core.errors import ProviderError


class Progress(BaseModel):
    step: str


def test_sse_event_format() -> None:
    assert sse_event("token", {"text": "hi"}) == b'event: token\ndata: {"text":"hi"}\n\n'


async def _collect(src: AsyncIterator[tuple[str, BaseModel]]) -> list[bytes]:
    return [chunk async for chunk in sse_stream(src)]


async def test_sse_stream_converts_app_error() -> None:
    async def src() -> AsyncIterator[tuple[str, BaseModel]]:
        yield "progress", Progress(step="a")
        raise ProviderError("bedrock")

    out = await _collect(src())
    assert len(out) == 2
    assert out[1].startswith(b"event: error\n")
    data = json.loads(out[1].split(b"data: ", 1)[1])
    assert data["code"] == "provider_unavailable"
    assert data["request_id"]


async def test_sse_stream_hides_unexpected_errors() -> None:
    async def src() -> AsyncIterator[tuple[str, BaseModel]]:
        raise ValueError("a@b.io")
        yield  # pragma: no cover

    out = await _collect(src())
    assert len(out) == 1
    assert b'"code":"internal"' in out[0]
    assert b"a@b.io" not in out[0]


async def test_sse_stream_logs_app_errors_without_message() -> None:
    from structlog.testing import capture_logs

    from app.core.errors import AppError

    async def provider_fail() -> AsyncIterator[tuple[str, BaseModel]]:
        raise ProviderError("bedrock")
        yield  # pragma: no cover

    async def plain_fail() -> AsyncIterator[tuple[str, BaseModel]]:
        raise AppError("encrypted_pdf", "secret text", 422)
        yield  # pragma: no cover

    with capture_logs() as logs:
        await _collect(provider_fail())
        await _collect(plain_fail())

    warnings = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warnings) == 2
    assert warnings[0]["code"] == "provider_unavailable"
    assert warnings[0]["status"] == 503
    assert warnings[0]["provider"] == "bedrock"
    assert "request_id" in warnings[0]
    assert warnings[1]["code"] == "encrypted_pdf"
    assert "provider" not in warnings[1]
    assert all("message" not in entry and "secret text" not in str(entry) for entry in warnings)
