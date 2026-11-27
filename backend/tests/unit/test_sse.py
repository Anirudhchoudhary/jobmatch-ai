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


async def test_sse_stream_sends_keepalive_comments_while_the_source_is_silent() -> None:
    import asyncio

    async def slow() -> AsyncIterator[tuple[str, BaseModel]]:
        yield "progress", Progress(step="a")
        await asyncio.sleep(0.25)
        yield "done", Progress(step="b")

    out = [chunk async for chunk in sse_stream(slow(), keepalive_s=0.05)]
    assert out[0].startswith(b"event: progress\n")
    assert out[-1].startswith(b"event: done\n")
    pings = out[1:-1]
    assert len(pings) >= 2
    assert all(p == b": keep-alive\n\n" for p in pings)


async def test_sse_stream_does_not_delay_or_pad_prompt_events() -> None:
    import time

    async def fast() -> AsyncIterator[tuple[str, BaseModel]]:
        for step in ("a", "b", "c"):
            yield "progress", Progress(step=step)

    start = time.perf_counter()
    out = [chunk async for chunk in sse_stream(fast(), keepalive_s=10.0)]
    assert time.perf_counter() - start < 1.0
    assert len(out) == 3
    assert b": keep-alive" not in b"".join(out)


async def test_sse_stream_keeps_errors_after_keepalives() -> None:
    import asyncio

    async def fails_late() -> AsyncIterator[tuple[str, BaseModel]]:
        await asyncio.sleep(0.12)
        raise ProviderError("bedrock")
        yield  # pragma: no cover

    out = [chunk async for chunk in sse_stream(fails_late(), keepalive_s=0.05)]
    assert out[0] == b": keep-alive\n\n"
    assert out[-1].startswith(b"event: error\n")
    assert b'"code":"provider_unavailable"' in out[-1]


async def test_sse_stream_cancels_the_source_when_the_client_goes_away() -> None:
    import asyncio

    closed = asyncio.Event()

    async def endless() -> AsyncIterator[tuple[str, BaseModel]]:
        try:
            while True:
                yield "progress", Progress(step="a")
                await asyncio.sleep(0.01)
        finally:
            closed.set()

    stream = sse_stream(endless(), keepalive_s=1.0)
    assert (await anext(stream)).startswith(b"event: progress\n")
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1.0)


def test_keepalive_interval_defaults_to_15_seconds() -> None:
    from app.core.settings import Settings

    assert Settings.model_fields["sse_keepalive_s"].default == 15.0


async def test_sse_stream_ends_when_the_source_is_cancelled_from_inside() -> None:
    import asyncio

    async def cancelled() -> AsyncIterator[tuple[str, BaseModel]]:
        raise asyncio.CancelledError
        yield  # pragma: no cover

    out = await asyncio.wait_for(_collect_with(cancelled(), keepalive_s=0.02), timeout=1.0)
    assert out[-1].startswith(b"event: error\n")
    assert b'"code":"internal"' in out[-1]


async def _collect_with(
    src: AsyncIterator[tuple[str, BaseModel]], *, keepalive_s: float
) -> list[bytes]:
    return [chunk async for chunk in sse_stream(src, keepalive_s=keepalive_s)]
