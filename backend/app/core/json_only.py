"""Refuse state-changing /api requests that are not JSON (pure ASGI middleware).

A cross-site form or `fetch` with a "simple" content type (text/plain, form encoding) is sent
without a CORS preflight and carries the SameSite=Lax session cookie on top-level POSTs, so
requiring application/json closes that CSRF path for every cookie-authenticated route.
"""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import error_body
from app.core.request_id import request_id_var

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_JSON = "application/json"
# The local-only blob PUT (registered only in local/ci with the local blob backend).
_PDF_PUT_PREFIX = "/api/uploads/"
_PDF = "application/pdf"


def _media_type(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == b"content-type":
            media_type: str = value.decode("latin-1").split(";", 1)[0].strip().lower()
            return media_type
    return ""


def _allowed(scope: Scope) -> bool:
    if scope["method"] not in _UNSAFE_METHODS or not scope["path"].startswith("/api/"):
        return True
    media_type = _media_type(scope)
    if media_type == _JSON:
        return True
    is_pdf_put = scope["method"] == "PUT" and scope["path"].startswith(_PDF_PUT_PREFIX)
    return is_pdf_put and media_type == _PDF


class JsonOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _allowed(scope):
            await self.app(scope, receive, send)
            return
        rid = str(scope.get("state", {}).get("request_id") or request_id_var.get())
        response = JSONResponse(
            error_body("unsupported_media_type", "Send the request body as JSON.", rid),
            status_code=415,
        )
        await response(scope, receive, send)
