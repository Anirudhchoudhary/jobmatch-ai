"""Match analysis endpoint: streamed progress, then the (cached or fresh) analysis."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.api.sse import sse_response
from app.db.engine import get_sessionmaker
from app.graphs.match import analysis_events
from app.llm.providers import Providers, get_providers

router = APIRouter(prefix="/api/stream", tags=["analyses"])


@router.post("/analyses")
async def stream_analysis(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    providers: Annotated[Providers, Depends(get_providers)],
) -> StreamingResponse:
    return sse_response(
        analysis_events(user_id=user_id, sessionmaker=sessionmaker, analyst=providers.analyst)
    )
