"""Streaming ingestion: validate, parse, chunk, embed, store. Logs counts only, never text."""

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Literal
from uuid import UUID

import anyio
import structlog
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, ProviderError
from app.core.settings import Settings
from app.db.models import Chunk, Document
from app.ingestion.blobs import BlobStore, assert_owned_key
from app.ingestion.chunking import ChunkDraft, chunk_jd, chunk_resume
from app.ingestion.pdf import parse_pdf, validate_jd_text
from app.llm.embeddings import Embedder
from app.schemas.documents import DocumentReady, DocumentRequest, ProgressEvent

log = structlog.get_logger(__name__)

IngestEvent = tuple[str, BaseModel]
Step = Literal["validating", "parsing", "chunking", "embedding", "storing"]


def _progress(step: Step, status: Literal["started", "done"]) -> IngestEvent:
    return "progress", ProgressEvent(step=step, status=status)


async def _mark_failed(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID, doc_id: UUID, error_code: str
) -> None:
    """Fresh session: the original one may be in a failed state. Never masks the caller's error."""
    try:
        # Shielded so a cancelled request (client disconnect) can still record the failure.
        with anyio.CancelScope(shield=True):
            async with sessionmaker() as session:
                await session.execute(
                    update(Document)
                    .where(
                        Document.id == doc_id,
                        Document.user_id == user_id,
                        Document.status == "processing",  # never downgrade a committed row
                    )
                    .values(status="failed", error_code=error_code)
                )
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("ingest_mark_failed_error", document_id=str(doc_id), exc_info=exc)


async def _delete_blob(blobs: BlobStore, key: str) -> None:
    try:
        await blobs.delete(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("ingest_blob_delete_failed", exc_info=exc)


async def _record_parse_failure(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: UUID,
    request: DocumentRequest,
    data: bytes,
    error_code: str,
) -> None:
    """Keep a ``failed`` row for an unparseable upload (the text hash does not exist yet)."""
    try:
        async with sessionmaker() as session:
            session.add(
                Document(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    doc_type=request.doc_type,
                    source="upload",
                    filename=request.filename,
                    content_sha256=hashlib.sha256(data).hexdigest(),
                    status="failed",
                    error_code=error_code,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("ingest_record_failure_error", exc_info=exc)


async def _read_source(
    request: DocumentRequest,
    user_id: UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    settings: Settings,
) -> tuple[str, int | None, str]:
    """Return (text, page_count, sha256) for the request's source."""
    if request.upload_key is not None:
        data = await blobs.get(request.upload_key)
        try:
            parsed = await asyncio.to_thread(
                parse_pdf,
                data,
                max_bytes=settings.max_upload_bytes,
                max_pages=settings.max_pdf_pages,
            )
        except AppError as exc:
            await _record_parse_failure(sessionmaker, user_id, request, data, exc.code)
            raise
        return parsed.text, parsed.page_count, parsed.sha256
    assert request.text is not None  # noqa: S101  (guaranteed by DocumentRequest)
    text = validate_jd_text(
        request.text, min_chars=settings.jd_min_chars, max_chars=settings.jd_max_chars
    )
    return text, None, hashlib.sha256(text.encode()).hexdigest()


async def _find_reusable(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID, doc_type: str, sha: str
) -> DocumentReady | None:
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(Document.id, Document.content_sha256).where(
                    Document.user_id == user_id,
                    Document.doc_type == doc_type,
                    Document.status == "ready",
                )
            )
        ).first()
        if row is None or row.content_sha256 != sha:
            return None
        count = (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.user_id == user_id, Chunk.doc_id == row.id)
            )
        ).scalar_one()
    return DocumentReady(
        document_id=row.id,
        doc_type=doc_type,
        chunk_count=count,
        reused=True,
    )


async def _store(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: UUID,
    doc_id: UUID,
    doc_type: str,
    drafts: list[ChunkDraft],
    vectors: list[list[float]],
) -> None:
    async with sessionmaker() as session:
        # Serialize same-user, same-type ingests: the last writer wins deterministically.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k1), hashtext(:k2))"),
            {"k1": str(user_id), "k2": doc_type},
        )
        # The previous ready document goes only now, so a failed ingest never loses it.
        await session.execute(
            delete(Document).where(
                Document.user_id == user_id,
                Document.doc_type == doc_type,
                Document.status == "ready",
            )
        )
        session.add_all(
            Chunk(
                user_id=user_id,
                doc_id=doc_id,
                doc_type=doc_type,
                section=d.section,
                ordinal=d.ordinal,
                text=d.text,
                embedding=vec,
            )
            for d, vec in zip(drafts, vectors, strict=True)
        )
        await session.execute(
            update(Document)
            .where(Document.id == doc_id, Document.user_id == user_id)
            .values(status="ready", error_code=None)
        )
        await session.commit()


async def ingest_document(
    *,
    user_id: UUID,
    request: DocumentRequest,
    sessionmaker: async_sessionmaker[AsyncSession],
    blobs: BlobStore,
    embedder: Embedder,
    settings: Settings,
    source: Literal["sample"] | None = None,
) -> AsyncIterator[IngestEvent]:
    """``source`` is internal (not client-settable): only the bundled samples pass it."""
    doc_type = request.doc_type
    t0 = time.perf_counter()

    yield _progress("validating", "started")
    if request.upload_key is not None:
        assert_owned_key(request.upload_key, user_id)
    yield _progress("validating", "done")

    yield _progress("parsing", "started")
    text, page_count, sha = await _read_source(request, user_id, sessionmaker, blobs, settings)
    yield _progress("parsing", "done")

    reused = await _find_reusable(sessionmaker, user_id, doc_type, sha)
    if reused is not None:
        if request.upload_key is not None:
            await _delete_blob(blobs, request.upload_key)
        log.info("ingest_reused", doc_type=doc_type, chunk_count=reused.chunk_count)
        yield "done", reused
        return

    doc_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=doc_id,
                user_id=user_id,
                doc_type=doc_type,
                source=source or ("upload" if request.upload_key is not None else "text"),
                filename=request.filename,
                page_count=page_count,
                content_sha256=sha,
                status="processing",
            )
        )
        await session.commit()

    try:
        yield _progress("chunking", "started")
        drafts = chunk_resume(text) if doc_type == "resume" else chunk_jd(text)
        if not drafts:
            raise AppError("empty_document", "No usable text was found in that document.", 422)
        yield _progress("chunking", "done")

        yield _progress("embedding", "started")
        t_embed = time.perf_counter()
        vectors = await embedder.embed_documents([d.text for d in drafts])
        if len(vectors) != len(drafts):
            raise ProviderError("embedder")
        embed_s = time.perf_counter() - t_embed
        yield _progress("embedding", "done")

        yield _progress("storing", "started")
        await _store(sessionmaker, user_id, doc_id, doc_type, drafts, vectors)
        yield _progress("storing", "done")
    except BaseException as exc:
        # BaseException covers CancelledError and GeneratorExit (client disconnect).
        if isinstance(exc, AppError):
            code = exc.code
        elif isinstance(exc, Exception):
            code = "internal"
        else:
            code = "cancelled"
        await _mark_failed(sessionmaker, user_id, doc_id, code)
        raise

    if request.upload_key is not None:
        await _delete_blob(blobs, request.upload_key)
    log.info(
        "ingest_done",
        doc_type=doc_type,
        chunk_count=len(drafts),
        page_count=page_count,
        embed_s=round(embed_s, 3),
        total_s=round(time.perf_counter() - t0, 3),
    )
    yield (
        "done",
        DocumentReady(document_id=doc_id, doc_type=doc_type, chunk_count=len(drafts), reused=False),
    )
