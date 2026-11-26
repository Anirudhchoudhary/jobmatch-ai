"""Persistence for match analyses and the documents they are computed from.

Every query is scoped by ``user_id``. Only ``save_analysis`` commits. Analyses reference
documents with ``ON DELETE CASCADE``, so replacing a document drops its stale analyses.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Analysis, Chunk, Document
from app.retrieval.search import RetrievedChunk


@dataclass(frozen=True)
class LoadedDocuments:
    resume_doc_id: UUID
    jd_doc_id: UUID
    resume: list[RetrievedChunk]
    jd: list[RetrievedChunk]


async def _ready_doc_id(session: AsyncSession, user_id: UUID, doc_type: str) -> UUID | None:
    return (
        await session.execute(
            select(Document.id).where(
                Document.user_id == user_id,
                Document.doc_type == doc_type,
                Document.status == "ready",
            )
        )
    ).scalar_one_or_none()


async def _chunks(session: AsyncSession, user_id: UUID, doc_id: UUID) -> list[RetrievedChunk]:
    rows = await session.execute(
        select(Chunk)
        .where(Chunk.user_id == user_id, Chunk.doc_id == doc_id)
        .order_by(Chunk.ordinal)
    )
    return [
        RetrievedChunk(c.id, c.doc_id, c.doc_type, c.section, c.ordinal, c.text, 0.0)
        for c in rows.scalars()
    ]


async def load_documents(session: AsyncSession, user_id: UUID) -> LoadedDocuments:
    resume_id = await _ready_doc_id(session, user_id, "resume")
    jd_id = await _ready_doc_id(session, user_id, "jd")
    if resume_id is None or jd_id is None:
        raise AppError("documents_required", "Upload a resume and a job description first.", 409)
    return LoadedDocuments(
        resume_id,
        jd_id,
        await _chunks(session, user_id, resume_id),
        await _chunks(session, user_id, jd_id),
    )


async def get_cached_analysis(
    session: AsyncSession, user_id: UUID, resume_doc_id: UUID, jd_doc_id: UUID, analyst: str
) -> tuple[UUID, dict[str, Any]] | None:
    row = (
        await session.execute(
            select(Analysis.id, Analysis.result).where(
                Analysis.user_id == user_id,
                Analysis.resume_doc_id == resume_doc_id,
                Analysis.jd_doc_id == jd_doc_id,
                Analysis.analyst == analyst,
            )
        )
    ).one_or_none()
    return None if row is None else (row.id, row.result)


async def save_analysis(
    session: AsyncSession,
    *,
    user_id: UUID,
    resume_doc_id: UUID,
    jd_doc_id: UUID,
    analyst: str,
    result: dict[str, Any],
) -> UUID:
    ins = insert(Analysis).values(
        id=uuid4(),
        user_id=user_id,
        resume_doc_id=resume_doc_id,
        jd_doc_id=jd_doc_id,
        analyst=analyst,
        result=result,
    )
    stmt = ins.on_conflict_do_update(
        constraint="uq_analyses_docs", set_={"result": ins.excluded.result}
    ).returning(Analysis.id)
    analysis_id: UUID = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return analysis_id


async def get_analysis(
    session: AsyncSession, user_id: UUID, analysis_id: UUID
) -> tuple[UUID, UUID, dict[str, Any]] | None:
    row = (
        await session.execute(
            select(Analysis.resume_doc_id, Analysis.jd_doc_id, Analysis.result).where(
                Analysis.id == analysis_id, Analysis.user_id == user_id
            )
        )
    ).one_or_none()
    return None if row is None else (row.resume_doc_id, row.jd_doc_id, row.result)


async def latest_analysis(
    session: AsyncSession, user_id: UUID, resume_doc_id: UUID, jd_doc_id: UUID
) -> tuple[UUID, dict[str, Any]] | None:
    row = (
        await session.execute(
            select(Analysis.id, Analysis.result)
            .where(
                Analysis.user_id == user_id,
                Analysis.resume_doc_id == resume_doc_id,
                Analysis.jd_doc_id == jd_doc_id,
            )
            .order_by(Analysis.created_at.desc())
            .limit(1)
        )
    ).one_or_none()
    return None if row is None else (row.id, row.result)
