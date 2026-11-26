"""Grounded single-turn chat: retrieve -> rerank -> (refuse | generate) -> finalize.

Nodes are small and talk to the client only through ``get_stream_writer()`` custom events
(``progress`` and ``token``), so later phases can insert nodes (e.g. guardrails) between
them without touching the API layer.
"""

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

import anthropic
import structlog
from langchain_core.exceptions import ModelError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from app.core.errors import ProviderError
from app.core.request_id import request_id_var
from app.graphs.citations import REFUSAL_TEXT, extract_citations, format_context, label_context
from app.llm.errors import log_provider_failure
from app.llm.prompts import Prompt
from app.retrieval.search import RetrievedChunk
from app.retrieval.service import Retriever
from app.schemas.chat import ChatDone, ChatProgress, TokenEvent

log = structlog.get_logger(__name__)


class ChatState(TypedDict):
    user_id: UUID
    question: str
    chunks: list[RetrievedChunk]
    answer: str
    refused: bool


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _text_parts(content: str | list[str | dict[str, Any]]) -> list[str]:
    """User-visible text of a streamed chunk; thinking and tool blocks are dropped."""
    if isinstance(content, str):
        return [content] if content else []
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        # thinking / redacted_thinking / tool_use / anything else is never shown.
    return [p for p in parts if p]


def build_messages(
    prompt: Prompt, question: str, chunks: Sequence[RetrievedChunk]
) -> list[BaseMessage]:
    context = format_context(label_context(chunks))
    human = f"<context>\n{context}\n</context>\n\nQuestion: {question}"
    return [SystemMessage(prompt.text), HumanMessage(human)]


def build_chat_graph(
    retriever: Retriever,
    chat_model: BaseChatModel,
    prompt: Prompt,
    *,
    min_relevance: float,
) -> CompiledStateGraph[ChatState, None, ChatState, ChatState]:
    async def retrieve(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "progress", "step": "retrieving"})
        start = time.perf_counter()
        chunks = await retriever.candidates(state["user_id"], state["question"])
        log.info("chat_retrieved", candidate_count=len(chunks), ms=_ms(start))
        return {"chunks": chunks}

    async def rerank(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "progress", "step": "reranking"})
        start = time.perf_counter()
        chunks = await retriever.rerank(state["question"], state["chunks"])
        log.info("chat_reranked", chunk_count=len(chunks), ms=_ms(start))
        return {"chunks": chunks}

    def route(state: ChatState) -> Literal["refuse", "generate"]:
        chunks = state["chunks"]
        if not chunks:
            return "refuse"
        scored = [c.relevance for c in chunks if c.relevance is not None]
        if len(scored) == len(chunks) and all(r < min_relevance for r in scored):
            return "refuse"
        return "generate"

    async def refuse(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "token", "text": REFUSAL_TEXT})
        return {"answer": REFUSAL_TEXT, "refused": True}

    async def generate(state: ChatState) -> dict[str, Any]:
        writer = get_stream_writer()
        writer({"type": "progress", "step": "generating"})
        start = time.perf_counter()
        messages = build_messages(prompt, state["question"], state["chunks"])
        answer: list[str] = []
        try:
            async for chunk in chat_model.astream(messages):
                for text in _text_parts(chunk.content):
                    answer.append(text)
                    writer({"type": "token", "text": text})
        except (anthropic.APIError, ModelError) as exc:
            log_provider_failure("anthropic", exc)
            raise ProviderError("anthropic") from exc
        log.info("chat_generated", ms=_ms(start))
        return {"answer": "".join(answer), "refused": False}

    async def finalize(state: ChatState) -> dict[str, Any]:
        log.info(
            "chat_finalized",
            prompt_version=prompt.version,
            chunk_count=len(state["chunks"]),
            refused=state["refused"],
        )
        return {}

    graph = StateGraph(ChatState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("rerank", rerank)
    graph.add_node("refuse", refuse)
    graph.add_node("generate", generate)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "rerank")
    graph.add_conditional_edges("rerank", route, ["refuse", "generate"])
    graph.add_edge("refuse", "finalize")
    graph.add_edge("generate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


async def chat_events(
    graph: CompiledStateGraph[ChatState, None, ChatState, ChatState],
    state: ChatState,
    *,
    prompt_version: str,
) -> AsyncIterator[tuple[str, BaseModel]]:
    """Map graph stream parts to SSE events: ``progress``, ``token``, then ``done``."""
    start = time.perf_counter()
    final: ChatState = state
    async for mode, raw in graph.astream(state, stream_mode=["custom", "values"]):
        if mode == "values":
            final = cast(ChatState, raw)
            continue
        part = cast(dict[str, Any], raw)
        if part["type"] == "progress":
            yield "progress", ChatProgress(step=part["step"])
        elif part["type"] == "token":
            yield "token", TokenEvent(text=part["text"])
    citations, invalid = extract_citations(final["answer"], label_context(final["chunks"]))
    log.info(
        "chat_completed",
        prompt_version=prompt_version,
        citation_count=len(citations),
        invalid_citation_count=len(invalid),
        refused=final["refused"],
        ms=_ms(start),
    )
    yield (
        "done",
        ChatDone(
            answer=final["answer"],
            citations=citations,
            invalid_citations=invalid,
            refused=final["refused"],
            prompt_version=prompt_version,
            request_id=request_id_var.get(),
        ),
    )
