"""Structured model output with validation feedback, retry and model escalation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
import structlog
from langchain_core.exceptions import ModelError, OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from app.core.errors import AppError, ProviderError
from app.llm.errors import log_provider_failure

log = structlog.get_logger(__name__)

_MAX_FEEDBACK_LINES = 10
_MAX_LINE_CHARS = 300
_TRUNCATED_FEEDBACK = (
    "Your previous answer was cut off; be concise. Return only the JSON, keeping strings short."
)


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
    # The real parser wraps ValidationError in OutputParserException (whose text embeds the
    # completion), so unwrap the cause and never use str(error).
    cause = error.__cause__ if isinstance(error, OutputParserException) else error
    if isinstance(cause, ValidationError):
        return [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in cause.errors(include_input=False, include_url=False, include_context=False)
        ]
    return ["output was invalid or could not be parsed"]


def _truncated(raw: Any) -> bool:
    """The model hit ``max_tokens`` (Anthropic ``stop_reason``); retrying it is pointless."""
    metadata = raw.response_metadata if isinstance(raw, BaseMessage) else {}
    return bool(metadata.get("stop_reason") == "max_tokens")


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
    # Feedback carried to the next model after a truncated answer.
    carry: list[BaseMessage] = []
    for index in range(start_index, len(models)):
        runnable = models[index].with_structured_output(
            schema, method="json_schema", include_raw=True
        )
        base = [*messages, *carry]
        msgs = base
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
            truncated = _truncated(out.get("raw"))
            log.warning(
                "structured_attempt_failed",
                step=step,
                model_index=index,
                attempt=attempt,
                error_count=len(errors),
                escalated=index > start_index,
                truncated=truncated,
            )
            if truncated:
                carry = [HumanMessage(content=_TRUNCATED_FEEDBACK)]
                break
            msgs = [*base, *_feedback(out.get("raw"), errors)]
    raise StructuredOutputError
