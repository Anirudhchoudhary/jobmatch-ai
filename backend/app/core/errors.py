"""Error envelope: ``{"error": {"code", "message", "request_id"}}``."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_id import request_id_var

log = structlog.get_logger(__name__)


class AppError(Exception):
    """An expected failure whose ``message`` is safe to show to the user."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ProviderError(AppError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            "provider_unavailable",
            "A model provider is unavailable right now. Please try again shortly.",
            503,
        )
        self.provider = provider


def error_body(code: str, message: str, request_id: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _request_id(request: Request) -> str:
    # The contextvar is already reset when ServerErrorMiddleware handles a 500.
    state_id = request.scope.get("state", {}).get("request_id")
    return str(state_id) if state_id else request_id_var.get()


def _respond(request: Request, code: str, message: str, status: int) -> JSONResponse:
    rid = _request_id(request)
    return JSONResponse(
        error_body(code, message, rid), status_code=status, headers={"X-Request-ID": rid}
    )


_HTTP_ERRORS = {
    404: ("not_found", "Not found."),
    405: ("method_not_allowed", "Method not allowed."),
}


def install_error_handlers(app: FastAPI) -> None:
    async def on_app_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, AppError)  # noqa: S101
        return _respond(request, exc.code, exc.message, exc.status)

    async def on_validation_error(request: Request, _exc: Exception) -> JSONResponse:
        return _respond(request, "invalid_request", "The request was not valid.", 422)

    async def on_http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)  # noqa: S101
        code, message = _HTTP_ERRORS.get(exc.status_code, ("http_error", "The request failed."))
        response = _respond(request, code, message, exc.status_code)
        # Keep protocol headers such as Allow on 405.
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # exc_info is redacted by the logging pipeline; never echoed to the client.
        log.error("unhandled_error", request_id=_request_id(request), exc_info=exc)
        return _respond(request, "internal", "Something went wrong.", 500)

    app.add_exception_handler(AppError, on_app_error)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(Exception, on_unhandled)
