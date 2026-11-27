from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis.repo import (
    get_analysis,
    get_cached_analysis,
    latest_analysis,
    load_documents,
    save_analysis,
)
from app.core.errors import AppError
from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import User
from app.ingestion.blobs import LocalBlobStore
from app.ingestion.service import ingest_document
from app.llm.embeddings import HashEmbedder
from app.schemas.documents import DocumentRequest
from tests.fixtures.documents import JD, RESUME
from tests.fixtures.pdfs import make_pdf

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]
RESUME_B = RESUME.replace("Acme Corp", "Globex Inc").replace("Jane Doe", "John Roe")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def _new_user(sm: async_sessionmaker[AsyncSession]) -> UUID:
    uid = uuid4()
    async with sm() as s:
        s.add(User(id=uid))
        await s.commit()
    return uid


@pytest.fixture
async def user_id(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[UUID]:
    uid = await _new_user(sm)
    yield uid
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        await s.commit()


@pytest.fixture
async def other_user_id(sm: async_sessionmaker[AsyncSession]) -> AsyncIterator[UUID]:
    uid = await _new_user(sm)
    yield uid
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        await s.commit()


async def _ingest(
    sm: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    user_id: UUID,
    doc_type: str,
    resume_text: str = RESUME,
) -> None:
    blobs = LocalBlobStore(tmp_path)
    if doc_type == "resume":
        key = f"uploads/{user_id}/{uuid4()}.pdf"
        await blobs.put(key, make_pdf([resume_text]))
        request = DocumentRequest(doc_type="resume", upload_key=key)
    else:
        request = DocumentRequest(doc_type="jd", text=JD)
    async for _ in ingest_document(
        user_id=user_id,
        request=request,
        sessionmaker=sm,
        blobs=blobs,
        embedder=HashEmbedder(),
        settings=get_settings(),
    ):
        pass


async def _seed(sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID) -> None:
    await _ingest(sm, tmp_path, user_id, "resume")
    await _ingest(sm, tmp_path, user_id, "jd")


async def test_load_documents_requires_both(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID
) -> None:
    await _ingest(sm, tmp_path, user_id, "jd")
    async with sm() as s:
        with pytest.raises(AppError) as exc:
            await load_documents(s, user_id)
    assert exc.value.code == "documents_required"
    assert exc.value.status == 409


async def test_load_documents_orders_chunks(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID
) -> None:
    await _seed(sm, tmp_path, user_id)
    async with sm() as s:
        docs = await load_documents(s, user_id)
    assert docs.resume and docs.jd
    for chunks, doc_id in ((docs.resume, docs.resume_doc_id), (docs.jd, docs.jd_doc_id)):
        ordinals = [c.ordinal for c in chunks]
        assert ordinals == sorted(ordinals)
        assert {c.doc_id for c in chunks} == {doc_id}
        assert all(c.score == 0.0 for c in chunks)
    assert {c.doc_type for c in docs.resume} == {"resume"}
    assert {c.doc_type for c in docs.jd} == {"jd"}


async def test_save_get_roundtrip_and_upsert(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID
) -> None:
    await _seed(sm, tmp_path, user_id)
    async with sm() as s:
        d = await load_documents(s, user_id)
        key = {"user_id": user_id, "resume_doc_id": d.resume_doc_id, "jd_doc_id": d.jd_doc_id}
        first = await save_analysis(s, **key, analyst="heuristic", result={"score": 10})
        second = await save_analysis(s, **key, analyst="heuristic", result={"score": 77})
        assert first == second
        got = await get_analysis(s, user_id, first)
        assert got == (d.resume_doc_id, d.jd_doc_id, {"score": 77})
        cached = await get_cached_analysis(s, user_id, d.resume_doc_id, d.jd_doc_id, "heuristic")
        assert cached == (first, {"score": 77})
        assert await get_cached_analysis(s, user_id, d.resume_doc_id, d.jd_doc_id, "claude") is None
        other = await save_analysis(s, **key, analyst="claude", result={"score": 5})
        assert other != first
        latest = await latest_analysis(s, user_id, d.resume_doc_id, d.jd_doc_id)
        assert latest == (other, {"score": 5})


async def test_cached_lookup_is_user_scoped(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID, other_user_id: UUID
) -> None:
    await _seed(sm, tmp_path, user_id)
    async with sm() as s:
        d = await load_documents(s, user_id)
        aid = await save_analysis(
            s,
            user_id=user_id,
            resume_doc_id=d.resume_doc_id,
            jd_doc_id=d.jd_doc_id,
            analyst="heuristic",
            result={"score": 1},
        )
        assert await get_analysis(s, other_user_id, aid) is None
        assert (
            await get_cached_analysis(s, other_user_id, d.resume_doc_id, d.jd_doc_id, "heuristic")
            is None
        )
        assert await latest_analysis(s, other_user_id, d.resume_doc_id, d.jd_doc_id) is None


async def test_replacing_document_deletes_analyses(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path, user_id: UUID
) -> None:
    await _seed(sm, tmp_path, user_id)
    async with sm() as s:
        d = await load_documents(s, user_id)
        aid = await save_analysis(
            s,
            user_id=user_id,
            resume_doc_id=d.resume_doc_id,
            jd_doc_id=d.jd_doc_id,
            analyst="heuristic",
            result={"score": 1},
        )
    await _ingest(sm, tmp_path, user_id, "resume", RESUME_B)
    async with sm() as s:
        assert await get_analysis(s, user_id, aid) is None


def test_downgrade_upgrade_0004() -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    try:
        command.downgrade(cfg, "0003")
    finally:
        command.upgrade(cfg, "head")
