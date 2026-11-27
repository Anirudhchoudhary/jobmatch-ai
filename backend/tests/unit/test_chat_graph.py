import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import anthropic
import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.api.sse import sse_stream
from app.core.errors import ProviderError
from app.graphs.chat import ChatState, build_chat_graph, chat_events
from app.graphs.citations import REFUSAL_TEXT
from app.llm.prompts import load_prompt
from app.retrieval.search import RetrievedChunk
from app.schemas.chat import ChatDone, ChatProgress, TokenEvent

CHUNK = RetrievedChunk(uuid4(), uuid4(), "resume", "Experience", 0, "Alex built Kafka.", 0.9)
QUESTION = "ZQXQUESTION what did Alex use?"


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks

    async def candidates(self, user_id: object, query: str) -> list[RetrievedChunk]:
        return list(self.chunks)

    async def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        return chunks


class ScriptedModel(BaseChatModel):
    """Streams the given chunk contents, or raises ``error`` once streaming starts."""

    contents: list[Any] = []
    error: BaseException | None = None
    seen: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise AssertionError("use astream")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.seen.append(messages)
        if self.error is not None:
            raise self.error
        for content in self.contents:
            yield ChatGenerationChunk(message=AIMessageChunk(content=content))


class ExplodingModel(GenericFakeChatModel):
    def _generate(self, *a: Any, **kw: Any) -> ChatResult:  # pragma: no cover
        raise AssertionError("model must not be called")

    async def _astream(self, *a: Any, **kw: Any) -> AsyncIterator[ChatGenerationChunk]:
        raise AssertionError("model must not be called")
        yield  # pragma: no cover


def fake_model(text: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(text)]))


def state() -> ChatState:
    return {"user_id": uuid4(), "question": QUESTION, "chunks": [], "answer": "", "refused": False}


async def run(retriever: FakeRetriever, model: BaseChatModel) -> list[tuple[str, BaseModel]]:
    prompt = load_prompt("chat_system")
    graph = build_chat_graph(retriever, model, prompt, min_relevance=0.2)  # type: ignore[arg-type]
    return [ev async for ev in chat_events(graph, state(), prompt_version=prompt.version)]


def steps(events: list[tuple[str, BaseModel]]) -> list[str]:
    return [e.step for n, e in events if isinstance(e, ChatProgress)]


def tokens(events: list[tuple[str, BaseModel]]) -> list[str]:
    return [e.text for n, e in events if isinstance(e, TokenEvent)]


async def test_streams_progress_tokens_done() -> None:
    events = await run(FakeRetriever([CHUNK]), fake_model("Alex used Kafka [r1]."))
    names = [n for n, _ in events]
    assert names[:3] == ["progress", "progress", "progress"]
    assert steps(events) == ["retrieving", "reranking", "generating"]
    assert names[-1] == "done"
    assert set(names[3:-1]) == {"token"} and len(names) >= 5
    done = events[-1][1]
    assert isinstance(done, ChatDone)
    assert "".join(tokens(events)) == done.answer == "Alex used Kafka [r1]."
    assert done.citations[0].chunk_id == CHUNK.chunk_id
    assert done.citations[0].label == "r1"
    assert done.invalid_citations == []
    assert done.refused is False
    assert re.fullmatch(r"[0-9a-f]{8}", done.prompt_version)
    assert done.prompt_version == load_prompt("chat_system").version


async def test_refuses_without_calling_model_when_nothing_retrieved() -> None:
    events = await run(FakeRetriever([]), ExplodingModel(messages=iter([])))
    done = events[-1][1]
    assert isinstance(done, ChatDone)
    assert done.answer == REFUSAL_TEXT
    assert done.refused is True
    assert done.citations == []
    assert "generating" not in steps(events)
    assert tokens(events) == [REFUSAL_TEXT]


async def test_refuses_below_min_relevance() -> None:
    low = [replace(CHUNK, relevance=0.05), replace(CHUNK, chunk_id=uuid4(), relevance=0.05)]
    events = await run(FakeRetriever(low), ExplodingModel(messages=iter([])))
    done = events[-1][1]
    assert isinstance(done, ChatDone)
    assert done.refused is True
    assert done.answer == REFUSAL_TEXT
    assert "generating" not in steps(events)


async def test_one_relevant_chunk_is_enough() -> None:
    mixed = [replace(CHUNK, relevance=0.05), replace(CHUNK, chunk_id=uuid4(), relevance=0.5)]
    events = await run(FakeRetriever(mixed), fake_model("Kafka [r2]"))
    done = events[-1][1]
    assert isinstance(done, ChatDone)
    assert done.refused is False


async def _sse(model: BaseChatModel) -> list[tuple[str, dict[str, Any]]]:
    prompt = load_prompt("chat_system")
    graph = build_chat_graph(FakeRetriever([CHUNK]), model, prompt, min_relevance=0.2)  # type: ignore[arg-type]
    out = []
    async for raw in sse_stream(chat_events(graph, state(), prompt_version=prompt.version)):
        head, body = raw.decode().split("\ndata: ", 1)
        out.append((head.removeprefix("event: "), json.loads(body)))
    return out


async def test_model_failure_emits_error_event() -> None:
    out = await _sse(ScriptedModel(error=ProviderError("anthropic")))
    assert out[-1][0] == "error"
    assert out[-1][1]["code"] == "provider_unavailable"


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")),
        anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")),
        anthropic.InternalServerError(
            "boom",
            response=httpx.Response(500, request=httpx.Request("POST", "https://x")),
            body=None,
        ),
    ],
)
async def test_anthropic_errors_become_provider_unavailable(error: BaseException) -> None:
    with capture_logs() as logs:
        out = await _sse(ScriptedModel(error=error))
    assert out[-1][0] == "error"
    assert out[-1][1]["code"] == "provider_unavailable"
    warning = next(e for e in logs if e["event"] == "sse_app_error")
    assert warning["provider"] == "anthropic"


async def test_anthropic_error_logs_type_and_status_without_message() -> None:
    error = anthropic.InternalServerError(
        "ZQXBOOM",
        response=httpx.Response(529, request=httpx.Request("POST", "https://x")),
        body=None,
    )
    with capture_logs() as logs:
        await _sse(ScriptedModel(error=error))
    (entry,) = [e for e in logs if e["event"] == "provider_call_failed"]
    assert entry["log_level"] == "warning"
    assert entry["provider"] == "anthropic"
    assert entry["error_type"] == "InternalServerError"
    assert entry["status_code"] == 529
    assert "ZQXBOOM" not in str(logs)


async def test_thinking_blocks_not_streamed() -> None:
    model = ScriptedModel(
        contents=[
            [{"type": "thinking", "thinking": "secret plan", "index": 0}],
            [{"type": "text", "text": "Hi [r1]", "index": 1}],
            [{"type": "redacted_thinking", "data": "secret plan", "index": 2}],
            " more",
        ]
    )
    events = await run(FakeRetriever([CHUNK]), model)
    assert all("secret plan" not in t for t in tokens(events))
    assert tokens(events) == ["Hi [r1]", " more"]
    done = events[-1][1]
    assert isinstance(done, ChatDone)
    assert done.answer == "Hi [r1] more"
    assert [c.label for c in done.citations] == ["r1"]


async def test_messages_are_system_then_one_human_with_context() -> None:
    model = ScriptedModel(contents=["ok"], seen=[])
    await run(FakeRetriever([CHUNK]), model)
    (messages,) = model.seen
    assert [m.type for m in messages] == ["system", "human"]
    assert messages[0].content == load_prompt("chat_system").text
    human = messages[1]
    assert isinstance(human, HumanMessage)
    assert '<chunk label="r1" doc="resume" section="Experience">Alex built Kafka.</chunk>' in str(
        human.content
    )
    assert QUESTION in str(human.content)


async def test_logs_do_not_contain_question_or_answer() -> None:
    with capture_logs() as logs:
        await run(FakeRetriever([CHUNK]), fake_model("ZQXANSWER [r1]"))
    rendered = str(logs)
    assert "ZQXQUESTION" not in rendered and "ZQXANSWER" not in rendered
    final = next(e for e in logs if e["event"] == "chat_finalized")
    assert final["refused"] is False
    assert final["chunk_count"] == 1
    assert final["prompt_version"] == load_prompt("chat_system").version


def test_prompt_loader() -> None:
    p = load_prompt("chat_system")
    assert p.name == "chat_system"
    assert load_prompt("chat_system") is p
    assert REFUSAL_TEXT in p.text
    assert "<chunk>" in p.text or "<chunk" in p.text
