"""Grounded single-turn chat with guardrails.

START -> input_guards -> (refuse | cache_lookup -> (finalize on a hit |
retrieve -> rerank -> (refuse | generate -> output_guards -> cache_store))) -> finalize.

Nodes are small and talk to the client only through ``get_stream_writer()`` custom events
(``progress`` and ``token``); ``chat_events`` maps them, plus a final ``verdict``, to SSE.
"""

import time
from collections.abc import AsyncIterator, Sequence
from html import escape
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

import anthropic
import structlog
from langchain_core.exceptions import ModelError
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from app.cache.semantic import CachedAnswer, ChatCache, doc_set_key
from app.core.errors import ProviderError
from app.core.request_id import request_id_var
from app.graphs.citations import (
    EMPTY_ANSWER_TEXT,
    INJECTION_TEXT,
    OFF_TOPIC_TEXT,
    REFUSAL_TEXT,
    extract_citations,
    format_context,
    label_context,
)
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.factory import Guardrails
from app.guardrails.groundedness import split_claims
from app.llm.chat import ChatRunnable
from app.llm.errors import log_provider_failure
from app.llm.prompts import Prompt
from app.observability.tracing import trace_config
from app.observability.usage import current_usage
from app.retrieval.search import RetrievedChunk
from app.retrieval.service import Retriever
from app.schemas.chat import ChatDone, ChatProgress, TokenEvent, Verdict

log = structlog.get_logger(__name__)


class ChatState(TypedDict):
    user_id: UUID
    question: str
    chunks: list[RetrievedChunk]
    answer: str
    refused: bool
    guard_outcomes: list[GuardrailOutcome]
    blocked_by: str | None
    verdict: Verdict | None
    query_embedding: list[float] | None
    cache_key: str | None
    cached: CachedAnswer | None


_ALLOW_VERDICT = Verdict(grounded=True, action="allow", unsupported_claims=[], mode=None)


def _all_allow(outcomes: Sequence[GuardrailOutcome]) -> bool:
    return all(o.action == "allow" for o in outcomes)


def _block_text(outcome: GuardrailOutcome | None) -> str:
    if outcome is None:
        return REFUSAL_TEXT
    if outcome.guardrail == "topic":
        return OFF_TOPIC_TEXT
    if outcome.guardrail in ("injection_rules", "injection_classifier"):
        return INJECTION_TEXT
    if outcome.guardrail == "bedrock_guardrails" and outcome.reason == "bedrock:prompt_attack":
        return INJECTION_TEXT
    return REFUSAL_TEXT


def _verdict(outcomes: Sequence[GuardrailOutcome], answer: str) -> Verdict:
    """Verdict from the output-stage outcomes; no outcome means no output guard ran."""
    out = next((o for o in outcomes if o.guardrail == "groundedness"), None)
    if out is None:
        return Verdict(grounded=True, action="allow", unsupported_claims=[], mode=None)
    if out.action == "flag" and out.mode == "enforce":
        claims = split_claims(answer)
        texts = [claims[i].text for i in out.items if 0 <= i < len(claims)]
        return Verdict(grounded=False, action="flag", unsupported_claims=texts, mode=out.mode)
    return Verdict(grounded=True, action="allow", unsupported_claims=[], mode=out.mode)


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
    human = f"<context>\n{context}\n</context>\n\nQuestion: {escape(question, quote=False)}"
    return [SystemMessage(prompt.text), HumanMessage(human)]


def build_chat_graph(
    retriever: Retriever,
    chat_model: ChatRunnable,
    prompt: Prompt,
    *,
    min_relevance: float,
    guardrails: Guardrails,
    cache: ChatCache | None = None,
    cache_model: str = "",
) -> CompiledStateGraph[ChatState, None, ChatState, ChatState]:
    async def input_guards(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "progress", "step": "checking_input"})
        ctx = GuardContext(
            "chat_input", (state["question"],), request_id_var.get(), state["user_id"]
        )
        result = await guardrails.chat_input.run(ctx)
        update: dict[str, Any] = {
            "question": result.texts[0],
            "guard_outcomes": list(result.outcomes),
        }
        if result.blocked:
            blocking = [o for o in result.outcomes if o.action == "block" and o.mode == "enforce"]
            update["blocked_by"] = blocking[-1].guardrail if blocking else "unknown"
        return update

    def after_input(state: ChatState) -> Literal["refuse", "cache_lookup"]:
        return "refuse" if state["blocked_by"] is not None else "cache_lookup"

    async def cache_lookup(state: ChatState) -> dict[str, Any]:
        # Anything a guard redacted or flagged bypasses the cache in both directions.
        if cache is None or not _all_allow(state["guard_outcomes"]):
            return {}
        doc_ids = await retriever.active_docs(state["user_id"])
        if not doc_ids:
            return {}
        embedding = await retriever.embed_query(state["question"])
        key = doc_set_key(
            doc_ids,
            embed_model=retriever.embed_model,
            prompt_version=prompt.version,
            model=cache_model,
        )
        update: dict[str, Any] = {"query_embedding": embedding, "cache_key": key}
        hit = await cache.lookup(state["user_id"], key, embedding)
        log.info("chat_cache_lookup", hit=hit is not None)
        if hit is None:
            return update
        writer = get_stream_writer()
        writer({"type": "progress", "step": "cache_hit"})
        writer({"type": "token", "text": hit.answer})
        usage = current_usage()
        if usage is not None:
            usage.cache_hit = True
        return {**update, "cached": hit, "answer": hit.answer, "refused": False}

    def after_cache(state: ChatState) -> Literal["finalize", "retrieve"]:
        return "finalize" if state.get("cached") is not None else "retrieve"

    async def retrieve(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "progress", "step": "retrieving"})
        start = time.perf_counter()
        embedding = state.get("query_embedding")
        if embedding is None:
            chunks = await retriever.candidates(state["user_id"], state["question"])
        else:
            chunks = await retriever.candidates(
                state["user_id"], state["question"], embedding=embedding
            )
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
        blocking = [
            o for o in state["guard_outcomes"] if o.action == "block" and o.mode == "enforce"
        ]
        text = _block_text(blocking[-1] if blocking and state["blocked_by"] else None)
        get_stream_writer()({"type": "token", "text": text})
        return {"answer": text, "refused": True}

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
        text = "".join(answer)
        if not text.strip():
            # Adaptive thinking can spend the whole token budget without writing any text.
            log.warning("chat_empty_answer")
            writer({"type": "token", "text": EMPTY_ANSWER_TEXT})
            return {"answer": EMPTY_ANSWER_TEXT, "refused": True}
        return {"answer": text, "refused": False}

    async def output_guards(state: ChatState) -> dict[str, Any]:
        get_stream_writer()({"type": "progress", "step": "checking_answer"})
        sources = tuple(
            f"{label}\t{chunk.text}" for label, chunk in label_context(state["chunks"]).items()
        )
        ctx = GuardContext(
            "output", (state["answer"],), request_id_var.get(), state["user_id"], sources
        )
        result = await guardrails.output.run(ctx)
        outcomes = [*state["guard_outcomes"], *result.outcomes]
        return {"guard_outcomes": outcomes, "verdict": _verdict(result.outcomes, state["answer"])}

    async def cache_store(state: ChatState) -> dict[str, Any]:
        verdict = state["verdict"]
        key = state.get("cache_key")
        embedding = state.get("query_embedding")
        if (
            cache is None
            or key is None
            or embedding is None
            or state["refused"]
            or not state["answer"].strip()
            or verdict is None
            or not verdict.grounded
            or not _all_allow(state["guard_outcomes"])
        ):
            return {}
        citations, invalid = extract_citations(state["answer"], label_context(state["chunks"]))
        answer = CachedAnswer(
            answer=state["answer"], citations=citations, invalid_citations=invalid
        )
        await cache.store(state["user_id"], key, embedding, answer)
        return {}

    async def finalize(state: ChatState) -> dict[str, Any]:
        log.info(
            "chat_finalized",
            prompt_version=prompt.version,
            chunk_count=len(state["chunks"]),
            refused=state["refused"],
            cached=state.get("cached") is not None,
        )
        return {}

    graph = StateGraph(ChatState)
    graph.add_node("input_guards", input_guards)
    graph.add_node("cache_lookup", cache_lookup)
    graph.add_node("retrieve", retrieve)
    graph.add_node("rerank", rerank)
    graph.add_node("refuse", refuse)
    graph.add_node("generate", generate)
    graph.add_node("output_guards", output_guards)
    graph.add_node("cache_store", cache_store)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "input_guards")
    graph.add_conditional_edges("input_guards", after_input, ["refuse", "cache_lookup"])
    graph.add_conditional_edges("cache_lookup", after_cache, ["finalize", "retrieve"])
    graph.add_edge("retrieve", "rerank")
    graph.add_conditional_edges("rerank", route, ["refuse", "generate"])
    graph.add_edge("refuse", "finalize")
    graph.add_edge("generate", "output_guards")
    graph.add_edge("output_guards", "cache_store")
    graph.add_edge("cache_store", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


async def chat_events(
    graph: CompiledStateGraph[ChatState, None, ChatState, ChatState],
    state: ChatState,
    *,
    prompt_version: str,
) -> AsyncIterator[tuple[str, BaseModel]]:
    """Map graph stream parts to SSE events: ``progress``, ``token``, ``verdict``, ``done``."""
    start = time.perf_counter()
    final: ChatState = state
    async for mode, raw in graph.astream(
        state,
        config=trace_config(
            name="chat",
            user_id=state["user_id"],
            tags=["chat", f"prompt:chat_system:{prompt_version}"],
        ),
        stream_mode=["custom", "values"],
    ):
        if mode == "values":
            final = cast(ChatState, raw)
            continue
        part = cast(dict[str, Any], raw)
        if part["type"] == "progress":
            yield "progress", ChatProgress(step=part["step"])
        elif part["type"] == "token":
            yield "token", TokenEvent(text=part["text"])
    cached = final.get("cached")
    if cached is not None:
        citations, invalid = list(cached.citations), list(cached.invalid_citations)
        verdict = _ALLOW_VERDICT
    else:
        citations, invalid = extract_citations(final["answer"], label_context(final["chunks"]))
        verdict = final["verdict"] or _ALLOW_VERDICT
    log.info(
        "chat_completed",
        prompt_version=prompt_version,
        citation_count=len(citations),
        invalid_citation_count=len(invalid),
        refused=final["refused"],
        cached=cached is not None,
        ms=_ms(start),
    )
    yield "verdict", verdict
    yield (
        "done",
        ChatDone(
            answer=final["answer"],
            citations=citations,
            invalid_citations=invalid,
            refused=final["refused"],
            prompt_version=prompt_version,
            request_id=request_id_var.get(),
            guardrails=[o for o in final["guard_outcomes"] if o.action != "allow"],
            guardrail_checks=list(final["guard_outcomes"]),
            blocked=final["blocked_by"] is not None,
            cached=cached is not None,
        ),
    )
