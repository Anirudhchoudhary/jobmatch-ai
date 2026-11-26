import json
import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.core.errors import ProviderError
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker, make_engine
from app.db.models import Chunk, Document, User
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.chat import make_chat_model
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.pdfs import make_pdf

pytestmark = pytest.mark.integration

SECTIONS = {"Summary", "Experience", "Education", "Skills", "Projects", "Certifications", "Other"}
RESUME = (
    "Jane Doe\nSummary\nBackend engineer with eight years of Python experience.\n\n"
    "Experience\nAcme Corp - Senior Engineer. Built FastAPI services and Postgres pipelines.\n"
    "Led a team of four engineers and shipped a search platform.\n\n"
    "Education\nBSc Computer Science, State University.\n\n"
    "Skills\nPython, SQL, FastAPI, Docker, AWS, Kubernetes, Terraform.\n"
)
RESUME_B = RESUME.replace("Acme Corp", "Globex Inc").replace("Jane Doe", "John Roe")
JD = "We are hiring a Python backend engineer to build FastAPI services and data pipelines. " * 3


class CountingEmbedder(HashEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return await super().embed_documents(texts)


class FailingEmbedder(HashEmbedder):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise ProviderError("bedrock")


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    out = []
    for block in body.strip().split("\n\n"):
        m = re.fullmatch(r"event: (\S+)\ndata: (.*)", block, re.S)
        assert m, block
        out.append((m.group(1), json.loads(m.group(2))))
    return out


class Env:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_id: UUID,
        sm: async_sessionmaker[AsyncSession],
        blobs: LocalBlobStore,
        embedder: CountingEmbedder,
    ) -> None:
        self.client, self.user_id, self.sm, self.blobs, self.embedder = (
            client,
            user_id,
            sm,
            blobs,
            embedder,
        )

    async def upload(self, data: bytes) -> str:
        resp = await self.client.post(
            "/api/uploads", json={"content_type": "application/pdf", "size_bytes": len(data)}
        )
        ticket = resp.json()
        put = await self.client.put(
            ticket["url"], content=data, headers={"Content-Type": "application/pdf"}
        )
        assert put.status_code == 204
        return str(ticket["key"])

    async def ingest(self, body: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
        resp = await self.client.post("/api/stream/documents", json=body)
        assert resp.status_code == 200
        return parse_sse(resp.text)

    async def ingest_pdf(self, pages: list[str], **kw: bool) -> list[tuple[str, dict[str, object]]]:
        key = await self.upload(make_pdf(pages, **kw))
        events = await self.ingest({"doc_type": "resume", "upload_key": key})
        self.last_key = key
        return events

    async def docs(self) -> list[Document]:
        async with self.sm() as s:
            rows = await s.execute(select(Document).where(Document.user_id == self.user_id))
            return list(rows.scalars())

    async def chunk_count(self, doc_id: UUID) -> int:
        async with self.sm() as s:
            return (
                await s.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.doc_id == doc_id)
                )
            ).scalar_one()

    last_key: str = ""


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def env(engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[Env]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid4()
    async with sm() as s:
        s.add(User(id=user_id))
        await s.commit()
    app = create_app()
    embedder = CountingEmbedder()
    blobs = LocalBlobStore(tmp_path)
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: blobs
    holder = {"embedder": embedder}
    app.dependency_overrides[get_providers] = lambda: Providers(
        holder["embedder"], NoopReranker(), make_chat_model(get_settings())
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        e = Env(client, user_id, sm, blobs, embedder)
        e.holder = holder  # type: ignore[attr-defined]
        yield e
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await s.commit()


async def test_ingest_resume_pdf_end_to_end(env: Env) -> None:
    events = await env.ingest_pdf([RESUME])
    name, data = events[-1]
    assert name == "done"
    assert data["chunk_count"] >= 3  # type: ignore[operator]
    assert data["reused"] is False
    doc_id = UUID(str(data["document_id"]))
    async with env.sm() as s:
        rows = (await s.execute(select(Chunk).where(Chunk.doc_id == doc_id))).scalars().all()
    assert len(rows) == data["chunk_count"]
    assert all(c.embedding is not None and len(c.embedding) == 1024 for c in rows)
    assert {c.section for c in rows} <= SECTIONS
    with pytest.raises(Exception, match="Upload not found"):
        await env.blobs.get(env.last_key)
    assert [d.status for d in await env.docs()] == ["ready"]


async def test_ingest_jd_text(env: Env) -> None:
    events = await env.ingest({"doc_type": "jd", "text": JD})
    name, data = events[-1]
    assert name == "done"
    assert data["doc_type"] == "jd"
    assert data["chunk_count"] >= 1  # type: ignore[operator]
    (doc,) = await env.docs()
    assert doc.source == "text"
    assert doc.status == "ready"


async def test_reingest_same_content_is_reused(env: Env) -> None:
    first = (await env.ingest({"doc_type": "jd", "text": JD}))[-1][1]
    calls = env.embedder.calls
    second = (await env.ingest({"doc_type": "jd", "text": JD}))[-1][1]
    assert second["reused"] is True
    assert second["document_id"] == first["document_id"]
    assert second["chunk_count"] == first["chunk_count"]
    assert env.embedder.calls == calls
    assert len(await env.docs()) == 1


async def test_replacing_resume_removes_old_chunks(env: Env) -> None:
    old = (await env.ingest_pdf([RESUME]))[-1][1]
    new = (await env.ingest_pdf([RESUME_B]))[-1][1]
    assert old["document_id"] != new["document_id"]
    docs = await env.docs()
    assert [(str(d.id), d.status) for d in docs] == [(new["document_id"], "ready")]
    assert await env.chunk_count(UUID(str(old["document_id"]))) == 0
    assert await env.chunk_count(UUID(str(new["document_id"]))) == new["chunk_count"]


async def test_failed_ingest_keeps_previous_document(env: Env) -> None:
    old = (await env.ingest_pdf([RESUME]))[-1][1]
    events = await env.ingest_pdf([RESUME_B], encrypt=True)
    name, data = events[-1]
    assert name == "error"
    assert data["code"] == "encrypted_pdf"
    by_status = {d.status: d for d in await env.docs()}
    assert str(by_status["ready"].id) == old["document_id"]
    assert by_status["failed"].error_code == "encrypted_pdf"
    assert await env.chunk_count(by_status["ready"].id) == old["chunk_count"]


async def test_ingest_rejects_foreign_upload_key(env: Env) -> None:
    key = f"uploads/{uuid4()}/{uuid4()}.pdf"
    events = await env.ingest({"doc_type": "resume", "upload_key": key})
    name, data = events[-1]
    assert name == "error"
    assert data["code"] == "upload_not_found"
    assert await env.docs() == []


async def test_ingest_embedder_failure_emits_error_event(env: Env) -> None:
    env.holder["embedder"] = FailingEmbedder()  # type: ignore[attr-defined]
    events = await env.ingest({"doc_type": "jd", "text": JD})
    name, data = events[-1]
    assert name == "error"
    assert data["code"] == "provider_unavailable"
    (doc,) = await env.docs()
    assert doc.status == "failed"
    assert doc.error_code == "provider_unavailable"


async def test_invalid_request_is_422(env: Env) -> None:
    resp = await env.client.post("/api/stream/documents", json={"doc_type": "resume", "text": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
