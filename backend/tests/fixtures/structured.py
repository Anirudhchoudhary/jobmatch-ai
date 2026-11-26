"""Scripted chat model for structured-output tests (no network)."""

import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field, ValidationError


@dataclass(frozen=True)
class Reply:
    """A scripted reply with raw ``response_metadata`` (e.g. ``stop_reason``). ``payload`` is
    a JSON dict, or raw text (e.g. a truncated completion) that fails to parse."""

    payload: dict[str, Any] | str
    response_metadata: dict[str, Any] = field(default_factory=dict)


class ScriptedStructuredModel(BaseChatModel):
    """Pops one scripted item per structured call: a dict (JSON payload), a ``Reply`` or an
    Exception."""

    model: str = "scripted"
    script: list[Any] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def __init__(self, script: list[dict[str, Any] | Reply | Exception], **kwargs: Any) -> None:
        super().__init__(script=list(script), **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted-structured"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])

    def with_structured_output(  # type: ignore[override]
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[Any, Any]:
        async def run(messages: Any) -> dict[str, Any]:
            self.calls.append(list(messages))
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            reply = item if isinstance(item, Reply) else Reply(item)
            text = reply.payload if isinstance(reply.payload, str) else json.dumps(reply.payload)
            raw = AIMessage(content=text, response_metadata=dict(reply.response_metadata))
            parsed: BaseModel | None = None
            error: Exception | None = None
            try:
                parsed = schema.model_validate_json(text)
            except ValidationError as exc:
                # Mirror PydanticOutputParser: the ValidationError is wrapped, text embeds output.
                msg = f"Failed to parse from completion {raw.text}. Got: {exc}"
                error = OutputParserException(msg)
                error.__cause__ = exc
            return {"raw": raw, "parsed": parsed, "parsing_error": error}

        def sync(_: Any) -> dict[str, Any]:
            raise NotImplementedError("async only")

        return RunnableLambda(sync, afunc=run)


def ladder() -> tuple[ScriptedStructuredModel, ScriptedStructuredModel]:
    """Unused (primary, strong) pair for tests that build ``Providers`` by hand."""
    return ScriptedStructuredModel([]), ScriptedStructuredModel([])
