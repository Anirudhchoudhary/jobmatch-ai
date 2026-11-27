"""Claude (Haiku) guards: a prompt-injection classifier and a topic classifier."""

import re
import time
from collections.abc import Sequence
from html import escape

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.guardrails.base import GuardContext, GuardrailResult
from app.llm.prompts import Prompt
from app.llm.structured import run_structured

_LABEL = re.compile(r"t([0-9]+)")


class FlaggedItem(BaseModel):
    label: str = Field(pattern=r"^t[0-9]+$")


class InjectionVerdicts(BaseModel):
    flagged: list[FlaggedItem]


class TopicVerdict(BaseModel):
    on_topic: bool


def _format_items(texts: Sequence[str]) -> str:
    return "\n".join(
        f'<item label="t{i}">{escape(t, quote=False)}</item>' for i, t in enumerate(texts, 1)
    )


class InjectionClassifier:
    name = "injection_classifier"
    provider = "anthropic"

    def __init__(self, models: Sequence[BaseChatModel], prompt: Prompt) -> None:
        self._models = models
        self._prompt = prompt

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        if not ctx.texts:
            return GuardrailResult(self.name, self.provider, "allow", "", 0)
        start = time.perf_counter()
        result = await run_structured(
            InjectionVerdicts,
            [
                SystemMessage(content=self._prompt.text),
                HumanMessage(content=_format_items(ctx.texts)),
            ],
            models=self._models,
            step="guard_injection",
            max_attempts_per_model=1,
        )
        indexes: set[int] = set()
        for item in result.value.flagged:
            match = _LABEL.fullmatch(item.label)
            if match and 1 <= int(match.group(1)) <= len(ctx.texts):
                indexes.add(int(match.group(1)) - 1)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not indexes:
            return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
        return GuardrailResult(
            self.name,
            self.provider,
            "block",
            "classifier:injection",
            latency_ms,
            items=tuple(sorted(indexes)),
        )


class TopicClassifier:
    name = "topic"
    provider = "anthropic"

    def __init__(self, models: Sequence[BaseChatModel], prompt: Prompt) -> None:
        self._models = models
        self._prompt = prompt

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        if not ctx.texts:
            return GuardrailResult(self.name, self.provider, "allow", "", 0)
        start = time.perf_counter()
        result = await run_structured(
            TopicVerdict,
            [
                SystemMessage(content=self._prompt.text),
                HumanMessage(content=_format_items(ctx.texts)),
            ],
            models=self._models,
            step="guard_topic",
            max_attempts_per_model=1,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        if result.value.on_topic:
            return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
        return GuardrailResult(self.name, self.provider, "block", "off_topic", latency_ms)
