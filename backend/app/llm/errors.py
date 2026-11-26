"""Diagnosable provider failures: log the error type and code, never messages or text."""

from typing import Any

import structlog
from botocore.exceptions import ClientError

log = structlog.get_logger(__name__)


def log_provider_failure(provider: str, exc: BaseException) -> None:
    """Warn with the exception class and its provider code/status only (no message text)."""
    fields: dict[str, Any] = {"provider": provider, "error_type": type(exc).__name__}
    if isinstance(exc, ClientError):
        fields["error_code"] = exc.response.get("Error", {}).get("Code")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        fields["status_code"] = status
    log.warning("provider_call_failed", **fields)
