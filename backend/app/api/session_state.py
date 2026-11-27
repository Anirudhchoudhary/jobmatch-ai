"""Read-only endpoints the web app uses to restore a session: documents, the latest analysis
and suggestions, and chunk text for citations. None of them spends an allowance."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.repo import get_suggestions, latest_for_active_documents
from app.api.deps import get_current_user_id
from app.core.errors import AppError
from app.core.request_id import request_id_var
from app.db.engine import get_sessionmaker
from app.db.models import Chunk, Document
from app.schemas.match import BulletSuggestions, MatchAnalysis

router = APIRouter(prefix="/api", tags=["session-state"])

Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
UserId = Annotated[UUID, Depends(get_current_user_id)]


class DocumentInfo(BaseModel):
    doc_id: UUID
    doc_type: str
    source: str
    filename: str | None
    status: str
    chunk_count: int
    quarantined_count: int
    created_at: datetime


class DocumentList(BaseModel):
    documents: list[DocumentInfo]


class ChunkText(BaseModel):
    chunk_id: UUID
    doc_type: str
    section: str
    text: str


@router.get("/documents")
async def list_documents(user_id: UserId, sessionmaker: Sessionmaker) -> DocumentList:
    quarantined = func.count(case((Chunk.quarantined.is_(True), 1)))
    stmt = (
        select(Document, func.count(Chunk.id), quarantined)
        .outerjoin(Chunk, Chunk.doc_id == Document.id)
        .where(Document.user_id == user_id, Document.status == "ready")
        .group_by(Document.id)
        .order_by(Document.doc_type)
    )
    async with sessionmaker() as session:
        rows = (await session.execute(stmt)).all()
    return DocumentList(
        documents=[
            DocumentInfo(
                doc_id=d.id,
                doc_type=d.doc_type,
                source=d.source,
                filename=d.filename,
                status=d.status,
                chunk_count=total,
                quarantined_count=held,
                created_at=d.created_at,
            )
            for d, total, held in rows
        ]
    )


async def _latest_stored(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID
) -> tuple[UUID, dict[str, Any]]:
    async with sessionmaker() as session:
        found = await latest_for_active_documents(session, user_id)
    if found is None:
        raise AppError("analysis_not_found", "Analysis not found.", 404)
    return found


@router.get("/analyses/latest")
async def latest_match_analysis(user_id: UserId, sessionmaker: Sessionmaker) -> MatchAnalysis:
    analysis_id, stored = await _latest_stored(sessionmaker, user_id)
    return MatchAnalysis(
        **stored, analysis_id=analysis_id, cached=True, request_id=request_id_var.get()
    )


@router.get("/suggestions/latest")
async def latest_bullet_suggestions(
    user_id: UserId, sessionmaker: Sessionmaker
) -> BulletSuggestions:
    analysis_id, _ = await _latest_stored(sessionmaker, user_id)
    async with sessionmaker() as session:
        stored = await get_suggestions(session, user_id, analysis_id)
    if stored is None:
        raise AppError("suggestions_not_found", "No suggestions yet.", 404)
    return BulletSuggestions(**stored, request_id=request_id_var.get())


@router.get("/chunks/{chunk_id}")
async def chunk_text(chunk_id: UUID, user_id: UserId, sessionmaker: Sessionmaker) -> ChunkText:
    stmt = (
        select(Chunk.id, Chunk.doc_type, Chunk.section, Chunk.text)
        .join(Document, Document.id == Chunk.doc_id)
        .where(
            Chunk.id == chunk_id,
            Chunk.user_id == user_id,
            Chunk.quarantined.is_(False),
            Document.user_id == user_id,
            Document.status == "ready",
        )
    )
    async with sessionmaker() as session:
        row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise AppError("chunk_not_found", "Chunk not found.", 404)
    return ChunkText(chunk_id=row.id, doc_type=row.doc_type, section=row.section, text=row.text)
