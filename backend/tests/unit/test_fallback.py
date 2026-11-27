from collections.abc import AsyncIterator, Iterator
from itertools import count
from typing import Any
from uuid import uuid4

import anthropic
import httpx2
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import BaseModel

from app.analysis.analyst import ClaudeAnalyst
from app.core.errors import ProviderError
from app.llm.chat import with_fallback
from app.llm.structured import StructuredOutputError, run_structured
from app.retrieval.search import RetrievedChunk
from tests.fixtures.structured import ScriptedStructuredModel

REQ = httpx2.Request("POST", "https://x")


def overloaded() -> anthropic.InternalServerError:
    return anthropic.InternalServerError(
        "overloaded", response=httpx2.Response(529, request=REQ), body=None
    )


def rate_limited() -> anthropic.RateLimitError:
    return anthropic.RateLimitError(
        "slow down", response=httpx2.Response(429, request=REQ), body=None
    )


def bad_request() -> anthropic.BadRequestError:
    return anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQ), body=None)


def _replies(text: str) -> Iterator[AIMessage]:
    return (AIMessage(content=text) for _ in count())


class FailingModel(GenericFakeChatModel):
    error: Any = None
    calls: int = 0

    async def _astream(  # type: ignore[override]
        self, *a: Any, **k: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.calls += 1
        raise self.error
        yield  # pragma: no cover

    async def _agenerate(self, *a: Any, **k: Any) -> ChatResult:
        self.calls += 1
        raise self.error


def failing(error: Exception) -> FailingModel:
    return FailingModel(messages=_replies("never"), error=error)


async def _stream_text(model: Any) -> str:
    return "".join([c.text async for c in model.astream([HumanMessage("hi")])])


async def test_chat_fallback_on_overloaded() -> None:
    fallback = GenericFakeChatModel(messages=_replies("ok"))
    model = with_fallback(failing(overloaded()), fallback)
    assert await _stream_text(model) == "ok"


async def test_chat_no_fallback_on_bad_request() -> None:
    primary = failing(bad_request())
    fallback = failing(overloaded())
    model = with_fallback(primary, fallback)
    with pytest.raises(anthropic.BadRequestError):
        await _stream_text(model)
    assert fallback.calls == 0


class Out(BaseModel):
    n: int


MSGS = [HumanMessage("go")]


async def test_structured_falls_back_to_other_model_on_rate_limit() -> None:
    a = ScriptedStructuredModel([rate_limited()])
    b = ScriptedStructuredModel([{"n": 1}])
    r = await run_structured(Out, MSGS, models=[a, b], step="t")
    assert r.model_index == 1
    assert b.calls[0] == MSGS  # no feedback carried on a provider error


async def test_structured_sonnet_falls_back_to_haiku() -> None:
    a = ScriptedStructuredModel([{"n": 2}])
    b = ScriptedStructuredModel([anthropic.APIConnectionError(request=REQ)])
    r = await run_structured(Out, MSGS, models=[a, b], step="t", start_index=1)
    assert r.model_index == 0


async def test_structured_both_unavailable_raises_provider_error() -> None:
    a = ScriptedStructuredModel([overloaded()])
    b = ScriptedStructuredModel([overloaded()])
    with pytest.raises(ProviderError):
        await run_structured(Out, MSGS, models=[a, b], step="t")


async def test_structured_does_not_retry_an_already_tried_model() -> None:
    a = ScriptedStructuredModel([{"n": "x"}, {"n": "x"}])  # validation escalation to b
    b = ScriptedStructuredModel([overloaded()])
    with pytest.raises(ProviderError):
        await run_structured(Out, MSGS, models=[a, b], step="t")
    assert len(a.calls) == 2


async def test_structured_non_retryable_raises_immediately() -> None:
    a = ScriptedStructuredModel([bad_request()])
    b = ScriptedStructuredModel([{"n": 1}])
    with pytest.raises(ProviderError):
        await run_structured(Out, MSGS, models=[a, b], step="t")
    assert b.calls == []


async def test_analyst_context_block_has_cache_control() -> None:
    chunk = RetrievedChunk(uuid4(), uuid4(), "jd", "Req", 0, "Needs Kafka", 0.9)
    item = {"skill": "Kafka", "text": "Kafka", "importance": "required", "source": "j1"}
    default = ScriptedStructuredModel([{"requirements": [item]}], model="m-default")
    strong = ScriptedStructuredModel([], model="m-strong")
    analyst = ClaudeAnalyst((default, strong), max_attempts=2, strong_input_chars=24_000)
    await analyst.extract_requirements({"j1": chunk})
    human: BaseMessage = default.calls[0][1]
    assert isinstance(human.content, list)
    assert human.content[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in human.content[1]
    assert "Needs Kafka" in human.content[0]["text"]
    assert "Extract the job requirements" in human.content[1]["text"]
    assert "Needs Kafka" in human.text  # pick_start_index measures joined text blocks


async def test_structured_fallback_target_exhausted_does_not_revisit_tried_model() -> None:
    a = ScriptedStructuredModel([{"n": "x"}, {"n": "x"}])
    b = ScriptedStructuredModel([overloaded()])
    # start at 1: b fails -> falls back to a (invalid twice) -> b must not be tried again
    with pytest.raises(StructuredOutputError):
        await run_structured(Out, MSGS, models=[a, b], step="t", start_index=1)
    assert len(b.calls) == 1
