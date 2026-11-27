"""Per-request usage ledger (tokens, cost, guardrail outcomes) and the LangChain callback."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import ChatGeneration, LLMResult

from app.core.settings import Settings, get_settings
from app.llm.pricing import llm_cost_usd

log = structlog.get_logger()


@dataclass
class RequestUsage:
    user_id: UUID | None = None
    models: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    guardrails: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def _note_model(self, model: str) -> None:
        if model not in self.models:
            self.models.append(model)

    def add_llm(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_write: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            self._note_model(model)
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cache_read_tokens += cache_read
            self.cache_write_tokens += cache_write
            self.cost_usd += cost_usd

    def add_cost(self, source: str, cost_usd: float) -> None:
        with self._lock:
            self._note_model(source)
            self.cost_usd += cost_usd

    def add_guardrail(self, entry: str) -> None:
        with self._lock:
            self.guardrails.append(entry)


_usage_var: ContextVar[RequestUsage | None] = ContextVar("request_usage", default=None)


def current_usage() -> RequestUsage | None:
    return _usage_var.get()


@contextmanager
def usage_scope() -> Iterator[RequestUsage]:
    usage = RequestUsage()
    token = _usage_var.set(usage)
    try:
        yield usage
    finally:
        _usage_var.reset(token)


def record_cost(source: str, cost_fn: Callable[[Settings], float]) -> None:
    """Add a non-LLM (Bedrock) cost to the open ledger. No-op without a scope; never raises."""
    usage = current_usage()
    if usage is None:
        return
    try:
        usage.add_cost(source, cost_fn(get_settings()))
    except Exception as exc:
        log.warning("usage_record_failed", source=source, error_type=type(exc).__name__)


class UsageCallback(AsyncCallbackHandler):
    """Adds each finished LLM call's tokens and cost to the open ledger."""

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = current_usage()
        if usage is None:
            return
        try:
            prices = get_settings().model_prices
            for generation in (g for batch in response.generations for g in batch):
                if not isinstance(generation, ChatGeneration):
                    continue
                message: Any = generation.message
                meta = getattr(message, "usage_metadata", None)
                if not meta:
                    continue
                details = meta.get("input_token_details") or {}
                # langchain-anthropic moves cache writes into the per-TTL keys and zeroes
                # the generic ``cache_creation`` key when it has them, so the sum never
                # double counts.
                cache_read = details.get("cache_read") or 0
                cache_write = (
                    (details.get("cache_creation") or 0)
                    + (details.get("ephemeral_5m_input_tokens") or 0)
                    + (details.get("ephemeral_1h_input_tokens") or 0)
                )
                input_tokens = meta.get("input_tokens") or 0
                output_tokens = meta.get("output_tokens") or 0
                model = (getattr(message, "response_metadata", None) or {}).get(
                    "model_name"
                ) or "unknown"
                usage.add_llm(
                    model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                    cost_usd=llm_cost_usd(
                        prices,
                        model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read=cache_read,
                        cache_write=cache_write,
                    ),
                )
        except Exception as exc:
            log.warning("usage_record_failed", error_type=type(exc).__name__)


USAGE_CALLBACK = UsageCallback()
