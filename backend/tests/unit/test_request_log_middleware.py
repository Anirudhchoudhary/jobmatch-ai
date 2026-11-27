import anyio
import httpx
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.core.request_id import RequestIdMiddleware
from app.observability.request_log import (
    DbRequestLogWriter,
    RequestLogEntry,
    RequestLogMiddleware,
    RequestLogWriter,
)
from app.observability.usage import current_usage


class ListWriter:
    def __init__(self) -> None:
        self.entries: list[RequestLogEntry] = []

    async def write(self, entry: RequestLogEntry) -> None:
        self.entries.append(entry)


def _add(cost: float) -> None:
    usage = current_usage()
    assert usage is not None
    usage.add_llm(
        "m1", input_tokens=10, output_tokens=5, cache_read=2, cache_write=1, cost_usd=cost
    )


async def ok(_: Request) -> Response:
    _add(0.25)
    return JSONResponse({"ok": True})


async def boom(_: Request) -> Response:
    raise RuntimeError("boom")


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True})


async def stream(_: Request) -> Response:
    async def gen():  # type: ignore[no-untyped-def]
        yield b"a"
        yield b"b"
        _add(0.5)
        yield b"c"

    return StreamingResponse(gen())


def _app(writer: RequestLogWriter) -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/ok", ok),
            Route("/api/uploads/{key:path}", ok),
            Route("/api/boom", boom),
            Route("/api/stream", stream),
            Route("/health", health),
        ]
    )
    app.add_middleware(RequestLogMiddleware, writer=writer)
    app.add_middleware(RequestIdMiddleware)
    return app


def _client(app: Starlette, raise_exc: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_exc)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_logs_api_request_with_usage() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        resp = await c.get("/api/ok")
    assert resp.status_code == 200
    (e,) = w.entries
    assert e.status == 200 and e.route == "/api/ok" and e.method == "GET"
    assert e.request_id == resp.headers["x-request-id"]
    assert e.models == ("m1",) and e.input_tokens == 10 and e.cost_usd == 0.25
    assert e.cache_read_tokens == 2 and e.cache_write_tokens == 1 and e.latency_ms >= 0


async def test_path_param_route_logs_template() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        assert (await c.get("/api/uploads/u1/secret.pdf")).status_code == 200
    (e,) = w.entries
    assert e.route == "/api/uploads/{key:path}"


async def test_unmatched_api_path_logs_constant() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        assert (await c.get("/api/jane@example.com")).status_code == 404
    (e,) = w.entries
    assert e.route == "<unmatched>"
    assert e.status == 404


async def test_skips_non_api_paths() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        assert (await c.get("/health")).status_code == 200
    assert w.entries == []


async def test_exception_logs_500_and_reraises() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        with pytest.raises(RuntimeError):
            await c.get("/api/boom")
    (e,) = w.entries
    assert e.status == 500


async def test_streaming_response_logged_after_body() -> None:
    w = ListWriter()
    async with _client(_app(w)) as c:
        resp = await c.get("/api/stream")
    assert resp.content == b"abc"
    (e,) = w.entries
    assert e.status == 200 and e.cost_usd == 0.5


async def test_client_disconnect_still_writes() -> None:
    w = ListWriter()
    app = _app(w)
    gate = anyio.Event()

    async def slow_stream(_: Request) -> Response:
        async def gen():  # type: ignore[no-untyped-def]
            yield b"a"
            await gate.wait()  # never set: stalls until cancelled
            yield b"b"

        return StreamingResponse(gen())

    app.router.routes.append(Route("/api/slow", slow_stream))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/slow",
        "raw_path": b"/api/slow",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("c", 1),
    }
    first_chunk = anyio.Event()

    async def receive() -> dict[str, str]:
        await anyio.sleep_forever()
        return {}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async def run(scope_: Scope, rcv: Receive, snd: Send) -> None:
        await app(scope_, rcv, snd)

    with anyio.CancelScope() as cancel:

        async def canceller() -> None:
            await first_chunk.wait()
            cancel.cancel()

        async with anyio.create_task_group() as tg:
            tg.start_soon(canceller)
            await run(scope, receive, send)  # type: ignore[arg-type]
    assert len(w.entries) == 1


class _FailingFactory:
    def __call__(self) -> async_sessionmaker[AsyncSession]:
        raise RuntimeError("db down")


async def test_writer_failure_never_breaks_response() -> None:
    writer = DbRequestLogWriter(_FailingFactory())
    with structlog.testing.capture_logs() as logs:
        async with _client(_app(writer)) as c:
            resp = await c.get("/api/ok")
    assert resp.status_code == 200
    failed = [entry for entry in logs if entry["event"] == "request_log_write_failed"]
    assert failed and failed[0]["error_type"] == "RuntimeError"
