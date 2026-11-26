"""Streaming document ingestion endpoint."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.api.sse import sse_response
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.guardrails.allowance import allowance
from app.ingestion.blobs import BlobStore, get_blob_store
from app.ingestion.service import ingest_document
from app.llm.providers import Providers, get_providers
from app.schemas.documents import DocumentRequest

router = APIRouter(prefix="/api/stream", tags=["documents"])


@router.post("/documents", dependencies=[Depends(allowance("uploads"))])
async def stream_documents(
    body: DocumentRequest,
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    providers: Annotated[Providers, Depends(get_providers)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    return sse_response(
        ingest_document(
            user_id=user_id,
            request=body,
            sessionmaker=sessionmaker,
            blobs=blobs,
            embedder=providers.embedder,
            settings=settings,
        )
    )
