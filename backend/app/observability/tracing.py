"""Optional Langfuse tracing. Off unless both keys are configured; never breaks a request."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import structlog
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler

from app.core.pii import redact_pii
from app.core.request_id import request_id_var
from app.core.settings import Settings

log = structlog.get_logger(__name__)

_enabled = False
_client: Langfuse | None = None


def mask_pii(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: redact every string inside dicts, lists and tuples."""
    if isinstance(data, str):
        return redact_pii(data)
    if isinstance(data, dict):
        return {k: mask_pii(data=v) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_pii(data=v) for v in data]
    if isinstance(data, tuple):
        return tuple(mask_pii(data=v) for v in data)
    return data


def init_tracing(settings: Settings) -> bool:
    global _enabled, _client
    _enabled, _client = False, None
    if settings.langfuse_public_key is None or settings.langfuse_secret_key is None:
        return False
    try:
        _client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            host=settings.langfuse_host,
            mask=mask_pii,
            environment=settings.env,
        )
    except Exception as exc:
        log.warning("tracing_init_failed", error_type=type(exc).__name__)
        return False
    _enabled = True
    log.info("tracing_enabled", host=settings.langfuse_host)
    return True


def trace_config(*, name: str, user_id: UUID, tags: Sequence[str]) -> RunnableConfig:
    config: RunnableConfig = {
        "run_name": name,
        "metadata": {
            "langfuse_user_id": str(user_id),
            "langfuse_session_id": request_id_var.get(),
            "langfuse_tags": list(tags),
        },
    }
    if _enabled:
        config["callbacks"] = [CallbackHandler()]
    return config


def flush_tracing() -> None:
    if not _enabled or _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:
        log.warning("tracing_flush_failed", error_type=type(exc).__name__)
