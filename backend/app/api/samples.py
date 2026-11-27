"""Try-it path: ingest the bundled fictional resume and job description for the current user."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.api.sse import sse_response
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.guardrails.factory import Guardrails, get_guardrails
from app.guardrails.spend import ensure_budget
from app.ingestion.blobs import BlobStore, get_blob_store, new_upload_key
from app.ingestion.service import ingest_document
from app.llm.embeddings import Embedder
from app.llm.providers import Providers, get_providers
from app.schemas.documents import DocumentRequest

router = APIRouter(prefix="/api", tags=["samples"])

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"


async def ingest_samples(
    *,
    user_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    embedder: Embedder,
    settings: Settings,
    guardrails: Guardrails,
) -> AsyncIterator[tuple[str, BaseModel]]:
    """Resume (from its PDF bytes) then JD (as text); yields both ingests' events."""
    key = new_upload_key(user_id)
    await blobs.put(key, (SAMPLES_DIR / "resume.pdf").read_bytes())
    jd_text = (SAMPLES_DIR / "jd.md").read_text(encoding="utf-8")
    requests = (
        DocumentRequest(doc_type="resume", upload_key=key, filename="sample-resume.pdf"),
        DocumentRequest(doc_type="jd", text=jd_text),
    )
    for request in requests:
        async for event in ingest_document(
            user_id=user_id,
            request=request,
            sessionmaker=sessionmaker,
            blobs=blobs,
            embedder=embedder,
            settings=settings,
            guardrails=guardrails,
            source="sample",
        ):
            yield event


@router.post("/samples")
async def post_samples(
    user_id: Annotated[UUID, Depends(get_current_user_id)],
    providers: Annotated[Providers, Depends(get_providers)],
    blobs: Annotated[BlobStore, Depends(get_blob_store)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    settings: Annotated[Settings, Depends(get_settings)],
    guardrails: Annotated[Guardrails, Depends(get_guardrails)],
) -> StreamingResponse:
    # Global spend cap only; samples are not charged against the per-user allowance.
    await ensure_budget(sessionmaker, user_id, settings=settings)
    return sse_response(
        ingest_samples(
            user_id=user_id,
            sessionmaker=sessionmaker,
            blobs=blobs,
            embedder=providers.embedder,
            settings=settings,
            guardrails=guardrails,
        )
    )
