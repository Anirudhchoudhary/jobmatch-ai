"""Structured model output with validation feedback, retry and model escalation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
import structlog
from langchain_core.exceptions import ModelError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from app.core.errors import AppError, ProviderError
from app.llm.errors import log_provider_failure

log = structlog.get_logger(__name__)

_MAX_FEEDBACK_LINES = 10
_MAX_LINE_CHARS = 300


class StructuredOutputError(AppError):
    def __init__(self) -> None:
        super().__init__(
            "model_output_invalid",
            "The model returned an unusable answer. Please try again.",
            502,
        )


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    model_index: int
    attempts: int


def pick_start_index(messages: Sequence[BaseMessage], threshold_chars: int) -> int:
    total = sum(len(m.text) for m in messages)
    return 1 if total > threshold_chars else 0


def _summarize(error: BaseException) -> list[str]:
    """Error locations and messages only: pydantic ``input`` values are never included."""
    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in error.errors(include_input=False, include_url=False, include_context=False)
        ]
    return ["output was invalid or could not be parsed"]


def _feedback(raw: Any, errors: list[str]) -> list[BaseMessage]:
    lines = [e[:_MAX_LINE_CHARS] for e in errors[:_MAX_FEEDBACK_LINES]]
    text = raw.text if isinstance(raw, BaseMessage) else str(raw or "")
    body = "Your previous answer was invalid:\n" + "\n".join(f"- {line}" for line in lines)
    return [
        AIMessage(content=text),
        HumanMessage(content=body + "\nReturn only corrected JSON."),
    ]


async def run_structured[T: BaseModel](
    schema: type[T],
    messages: Sequence[BaseMessage],
    *,
    models: Sequence[BaseChatModel],
    step: str,
    validate: Callable[[T], list[str]] = lambda _: [],
    max_attempts_per_model: int = 2,
    start_index: int = 0,
) -> StructuredResult[T]:
    for index in range(start_index, len(models)):
        runnable = models[index].with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        msgs = list(messages)
        for attempt in range(1, max_attempts_per_model + 1):
            try:
                out: dict[str, Any] = await runnable.ainvoke(msgs)  # type: ignore[assignment]
            except (anthropic.APIError, ModelError) as exc:
                log_provider_failure("anthropic", exc)
                raise ProviderError("anthropic") from exc
            parsed: T | None = out.get("parsed")
            if out.get("parsing_error") is not None:
                errors = _summarize(out["parsing_error"])
            elif parsed is None:
                errors = ["no structured output was returned"]
            else:
                errors = validate(parsed)
            if parsed is not None and not errors:
                return StructuredResult(parsed, index, attempt)
            log.warning(
                "structured_attempt_failed",
                step=step,
                model_index=index,
                attempt=attempt,
                error_count=len(errors),
                escalated=index > start_index,
            )
            msgs = [*messages, *_feedback(out.get("raw"), errors)]
    raise StructuredOutputError
