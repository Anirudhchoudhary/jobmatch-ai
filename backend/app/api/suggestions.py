"""Resume bullet suggestions: JSON (kept for compatibility) and streamed (SSE) endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.api.sse import sse_response
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.graphs.suggest import suggest, suggestion_events
from app.guardrails.allowance import consume_or_refuse
from app.guardrails.factory import Guardrails, get_guardrails
from app.llm.providers import Providers, get_providers
from app.schemas.match import BulletSuggestions, SuggestionRequest

router = APIRouter(tags=["suggestions"])


@router.post("/api/suggestions")
async def create_suggestions(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    providers: Annotated[Providers, Depends(get_providers)],
    settings: Annotated[Settings, Depends(get_settings)],
    guardrails: Annotated[Guardrails, Depends(get_guardrails)],
    body: SuggestionRequest | None = None,
) -> BulletSuggestions:
    await consume_or_refuse(sessionmaker, user_id, "suggestions", settings=settings)
    return await suggest(
        user_id=user_id,
        request=body or SuggestionRequest(),
        sessionmaker=sessionmaker,
        analyst=providers.analyst,
        limit=settings.suggest_max_bullets,
        recorder=guardrails.upload.recorder,
    )


@router.post("/api/stream/suggestions")
async def stream_suggestions(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    providers: Annotated[Providers, Depends(get_providers)],
    settings: Annotated[Settings, Depends(get_settings)],
    guardrails: Annotated[Guardrails, Depends(get_guardrails)],
    body: SuggestionRequest | None = None,
) -> StreamingResponse:
    # Charged before streaming, so a refusal is a plain HTTP error (as for the JSON endpoint).
    await consume_or_refuse(sessionmaker, user_id, "suggestions", settings=settings)
    return sse_response(
        suggestion_events(
            user_id=user_id,
            request=body or SuggestionRequest(),
            sessionmaker=sessionmaker,
            analyst=providers.analyst,
            limit=settings.suggest_max_bullets,
            recorder=guardrails.upload.recorder,
        ),
        keepalive_s=settings.sse_keepalive_s,
    )
