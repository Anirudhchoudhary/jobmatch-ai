"""Only CloudFront may reach /api/* (pure ASGI middleware).

CloudFront adds a secret `X-Origin-Verify` header to every origin request; the Lambda Function
URL is otherwise public, so a request without it did not come through the distribution. With no
token configured the middleware does nothing, so deployments without CloudFront keep working.
"""

import hmac

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import error_body
from app.core.request_id import request_id_var

log = structlog.get_logger(__name__)

HEADER = b"x-origin-verify"
_PROTECTED_PREFIX = "/api/"
# Set on the ASGI scope of a rejected request; the request log skips its DB row (anyone can
# send these straight to the Function URL, so storing them would let strangers fill the table).
REJECTED_SCOPE_KEY = "jobmatch.origin_rejected"


def origin_verified(headers: list[tuple[bytes, bytes]], token: str) -> bool:
    """True when the request carries the configured token (constant-time compare)."""
    expected = token.encode()
    return any(name == HEADER and hmac.compare_digest(value, expected) for name, value in headers)


class OriginVerifyMiddleware:
    def __init__(self, app: ASGIApp, token: str | None = None) -> None:
        self.app = app
        self.token = token or None  # an empty token counts as unset

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.token is None
            or scope["type"] != "http"
            or not scope["path"].startswith(_PROTECTED_PREFIX)
            or origin_verified(scope["headers"], self.token)
        ):
            await self.app(scope, receive, send)
            return
        rid = str(scope.get("state", {}).get("request_id") or request_id_var.get())
        log.warning("origin_verify_rejected", path=scope["path"], method=scope["method"])
        scope[REJECTED_SCOPE_KEY] = True
        response = JSONResponse(error_body("forbidden", "Forbidden.", rid), status_code=403)
        await response(scope, receive, send)
