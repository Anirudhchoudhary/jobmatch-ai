"""Request-id propagation (pure ASGI middleware + contextvar)."""

import re
import uuid
from contextvars import ContextVar

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_HEADER = b"x-request-id"


def _resolve_request_id(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == _HEADER:
            candidate: str = value.decode("latin-1")
            if _VALID_ID.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        rid = _resolve_request_id(scope)
        # scope state outlives this middleware, so outer error handlers can still read it.
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)
        structlog.contextvars.bind_contextvars(request_id=rid)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)
