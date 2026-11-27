import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import Chunk, Document, User

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]
VEC = [0.0] * 1024


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        user = User()
        s.add(user)
        await s.commit()
        s.info["user_id"] = user.id
        yield s
        await s.rollback()
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": s.info["user_id"]})
        await s.commit()


def _doc(user_id: uuid.UUID, status: str = "ready", doc_type: str = "resume") -> Document:
    return Document(
        user_id=user_id,
        doc_type=doc_type,
        source="text",
        content_sha256="0" * 64,
        status=status,
    )


async def test_tables_and_indexes_exist(session: AsyncSession) -> None:
    rows = await session.execute(text("SELECT indexname FROM pg_indexes"))
    names = {r[0] for r in rows}
    assert {"ix_chunks_embedding", "ix_chunks_tsv", "uq_documents_ready"} <= names


async def test_tsv_is_generated(session: AsyncSession) -> None:
    uid = session.info["user_id"]
    doc = _doc(uid)
    session.add(doc)
    await session.flush()
    session.add(
        Chunk(
            user_id=uid,
            doc_id=doc.id,
            doc_type="resume",
            section="experience",
            ordinal=0,
            text="Built Kafka pipelines",
            embedding=VEC,
        )
    )
    await session.commit()
    hit = await session.execute(
        text("SELECT tsv @@ to_tsquery('english','kafka') FROM chunks WHERE doc_id = :d"),
        {"d": doc.id},
    )
    assert hit.scalar_one() is True


async def test_one_ready_document_per_type(session: AsyncSession) -> None:
    uid = session.info["user_id"]
    session.add(_doc(uid))
    await session.commit()
    session.add(_doc(uid, status="processing"))
    await session.commit()
    session.add(_doc(uid))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_delete_document_cascades_chunks(session: AsyncSession) -> None:
    uid = session.info["user_id"]
    doc = _doc(uid)
    session.add(doc)
    await session.flush()
    session.add(
        Chunk(
            user_id=uid,
            doc_id=doc.id,
            doc_type="resume",
            section="skills",
            ordinal=0,
            text="Python",
            embedding=VEC,
        )
    )
    await session.commit()
    await session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
    await session.commit()
    left = (await session.execute(select(Chunk).where(Chunk.doc_id == doc.id))).all()
    assert left == []


def test_downgrade_then_upgrade() -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    try:
        command.downgrade(cfg, "0001")
    finally:
        command.upgrade(cfg, "head")
