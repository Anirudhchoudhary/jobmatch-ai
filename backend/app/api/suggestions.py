"""Resume bullet suggestions endpoint (JSON)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.graphs.suggest import suggest
from app.llm.providers import Providers, get_providers
from app.schemas.match import BulletSuggestions, SuggestionRequest

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])


@router.post("")
async def create_suggestions(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    providers: Annotated[Providers, Depends(get_providers)],
    body: SuggestionRequest | None = None,
) -> BulletSuggestions:
    return await suggest(
        user_id=user_id,
        request=body or SuggestionRequest(),
        sessionmaker=sessionmaker,
        analyst=providers.analyst,
        limit=get_settings().suggest_max_bullets,
    )
