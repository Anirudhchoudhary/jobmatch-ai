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
