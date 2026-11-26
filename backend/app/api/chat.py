"""Grounded chat endpoint (single-turn, stateless)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.api.sse import sse_response
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.graphs.chat import ChatState, build_chat_graph, chat_events
from app.guardrails.allowance import consume_or_refuse
from app.llm.prompts import load_prompt
from app.llm.providers import Providers, get_providers
from app.retrieval.service import Retriever, get_retriever
from app.schemas.chat import ChatRequest

router = APIRouter(prefix="/api/stream", tags=["chat"])


@router.post("/chat")
async def stream_chat(
    body: ChatRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    retriever: Annotated[Retriever, Depends(get_retriever)],
    providers: Annotated[Providers, Depends(get_providers)],
    settings: Annotated[Settings, Depends(get_settings)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
) -> StreamingResponse:
    await consume_or_refuse(sessionmaker, user_id, "chats", settings=settings)
    prompt = load_prompt("chat_system")
    graph = build_chat_graph(
        retriever, providers.chat_model, prompt, min_relevance=settings.min_relevance
    )
    state: ChatState = {
        "user_id": user_id,
        "question": body.question,
        "chunks": [],
        "answer": "",
        "refused": False,
    }
    return sse_response(chat_events(graph, state, prompt_version=prompt.version))
