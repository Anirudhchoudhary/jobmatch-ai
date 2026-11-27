"""DeepEval judge: Claude Sonnet 5.5 through LangChain's ChatAnthropic.

DeepEval phones home and reads ``.env`` implicitly, so the environment is pinned *before* the
import below (deepeval loads its settings and dotenv at import time):

* ``DEEPEVAL_TELEMETRY_OPT_OUT=YES``: no PostHog usage telemetry.
* ``DEEPEVAL_DISABLE_DOTENV=1``: deepeval would otherwise merge ``.env``/``.env.local`` from the
  cwd into ``os.environ``, which can hold real secrets and settings unrelated to evals.
* ``DEEPEVAL_UPDATE_WARNING_OPT_IN=NO``: no update check.
* ``CONFIDENT_API_KEY`` is dropped so a stray key can never turn on Confident AI uploads.
"""

import asyncio
import os
from collections.abc import Sequence
from typing import Any

os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "YES"
os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_UPDATE_WARNING_OPT_IN"] = "NO"
os.environ.pop("CONFIDENT_API_KEY", None)

import structlog  # noqa: E402
from deepeval.metrics import (  # noqa: E402
    AnswerRelevancyMetric,
    ContextualPrecisionMetric,
    ContextualRecallMetric,
    FaithfulnessMetric,
)
from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from deepeval.test_case import LLMTestCase  # noqa: E402
from langchain_anthropic import ChatAnthropic  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.core.settings import Settings  # noqa: E402
from app.observability.usage import USAGE_CALLBACK  # noqa: E402
from evals.results import ChatResult  # noqa: E402

log = structlog.get_logger(__name__)

JUDGE_METRICS = ("faithfulness", "answer_relevancy", "contextual_precision", "contextual_recall")


# misc: base is Any without deepeval; no-untyped-call: its __init_subclass__ with deepeval.
class ClaudeJudge(DeepEvalBaseLLM):  # type: ignore[misc,no-untyped-call,unused-ignore]
    """Adapter exposing a LangChain chat model to DeepEval metrics."""

    def __init__(self, model: BaseChatModel, name: str = "claude-sonnet-5-5") -> None:
        self._chat_model = model
        super().__init__(name)

    def load_model(self, *args: Any, **kwargs: Any) -> Any:
        return self._chat_model

    def get_model_name(self) -> str:
        return str(self.name)

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        if schema is None:
            return (await self._chat_model.ainvoke(prompt)).text
        runnable = self._chat_model.with_structured_output(schema, method="json_schema")
        return await runnable.ainvoke(prompt)

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.a_generate(prompt, schema))
        raise RuntimeError("ClaudeJudge.generate cannot run inside an event loop; use a_measure")


def make_judge(settings: Settings) -> ClaudeJudge:
    # No temperature: Sonnet 5.5 rejects any non-default value (langchain-anthropic raises).
    return ClaudeJudge(
        ChatAnthropic(
            model=settings.strong_chat_model,
            max_tokens=2000,
            timeout=60,
            max_retries=2,
            api_key=settings.anthropic_api_key,
            callbacks=[USAGE_CALLBACK],
        ),
        name=settings.strong_chat_model,
    )


def _build_metrics(judge: DeepEvalBaseLLM) -> dict[str, Any]:
    common: dict[str, Any] = {"model": judge, "include_reason": False, "async_mode": True}
    return {
        "faithfulness": FaithfulnessMetric(**common),
        "answer_relevancy": AnswerRelevancyMetric(**common),
        "contextual_precision": ContextualPrecisionMetric(**common),
        "contextual_recall": ContextualRecallMetric(**common),
    }


async def _judge_one(chat: ChatResult, judge: DeepEvalBaseLLM, sem: asyncio.Semaphore) -> None:
    case = LLMTestCase(
        input=chat.question,
        actual_output=chat.answer,
        expected_output=chat.expected_answer,
        retrieval_context=chat.retrieved,
    )
    for name, metric in _build_metrics(judge).items():
        async with sem:
            try:
                await metric.a_measure(case)
            except Exception as exc:
                log.warning("judge_metric_failed", metric=name, error_type=type(exc).__name__)
                continue
        if metric.score is not None:
            chat.judge[name] = float(metric.score)


async def judge_chats(
    chats: Sequence[ChatResult], judge: DeepEvalBaseLLM, *, concurrency: int = 4
) -> None:
    """Score every answerable chat whose answer the user saw (not refused, not blocked); flagged
    or ungrounded answers are judged too. Everything else is left untouched."""
    sem = asyncio.Semaphore(concurrency)
    todo = [c for c in chats if c.kind == "answerable" and not c.refused and c.blocked_by is None]
    await asyncio.gather(*(_judge_one(c, judge, sem) for c in todo))
