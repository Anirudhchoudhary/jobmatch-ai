from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis.heuristic import HeuristicAnalyst
from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker, make_engine
from app.db.models import Chunk, User
from app.graphs.citations import REFUSAL_TEXT
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.documents import JD, RESUME
from tests.fixtures.pdfs import make_pdf
from tests.fixtures.sse import parse_sse
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration

Events = list[tuple[str, dict[str, object]]]


class Env:
    def __init__(
        self, client: httpx.AsyncClient, user_id: UUID, sm: async_sessionmaker[AsyncSession]
    ) -> None:
        self.client, self.user_id, self.sm = client, user_id, sm

    async def post(self, path: str, body: dict[str, object]) -> Events:
        resp = await self.client.post(path, json=body)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        return parse_sse(resp.text)

    async def ingest_resume(self) -> None:
        data = make_pdf([RESUME])
        ticket = (
            await self.client.post(
                "/api/uploads", json={"content_type": "application/pdf", "size_bytes": len(data)}
            )
        ).json()
        put = await self.client.put(
            ticket["url"], content=data, headers={"Content-Type": "application/pdf"}
        )
        assert put.status_code == 204
        events = await self.post(
            "/api/stream/documents", {"doc_type": "resume", "upload_key": ticket["key"]}
        )
        assert events[-1][0] == "done"

    async def ingest_jd(self) -> None:
        events = await self.post("/api/stream/documents", {"doc_type": "jd", "text": JD})
        assert events[-1][0] == "done"


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
    blobs = LocalBlobStore(tmp_path)
    providers = Providers(
        HashEmbedder(),
        NoopReranker(),
        GenericFakeChatModel(messages=iter([AIMessage("Kafka [r1]")])),
        ladder(),
        HeuristicAnalyst(),
    )
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: blobs
    holder = {"providers": providers}
    app.dependency_overrides[get_providers] = lambda: holder["providers"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        e = Env(client, user_id, sm)
        e.holder = holder  # type: ignore[attr-defined]
        yield e
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await s.commit()


async def test_chat_api_end_to_end(env: Env) -> None:
    await env.ingest_resume()
    await env.ingest_jd()
    events = await env.post(
        "/api/stream/chat", {"question": "  What Python and FastAPI work has she done?  "}
    )
    names = [n for n, _ in events]
    assert names[:3] == ["progress"] * 3
    assert "token" in names
    name, done = events[-1]
    assert name == "done"
    assert done["answer"] == "Kafka [r1]"
    assert done["refused"] is False
    assert done["request_id"]
    citations = done["citations"]
    assert isinstance(citations, list) and len(citations) == 1
    assert citations[0]["doc_type"] == "resume"
    ids = [UUID(c["chunk_id"]) for c in citations]
    async with env.sm() as s:
        owned = (
            await s.execute(select(Chunk.id).where(Chunk.user_id == env.user_id, Chunk.id.in_(ids)))
        ).scalars()
        assert set(owned) == set(ids)


async def test_chat_without_documents_refuses(env: Env) -> None:
    events = await env.post("/api/stream/chat", {"question": "What Python work has she done?"})
    name, done = events[-1]
    assert name == "done"
    assert done["refused"] is True
    assert done["answer"] == REFUSAL_TEXT
    assert done["citations"] == []
    assert ("progress", {"step": "generating"}) not in events


async def test_chat_ignores_documents_from_another_embedder(env: Env) -> None:
    await env.ingest_resume()
    await env.ingest_jd()

    class OtherEmbedder(HashEmbedder):
        embed_model = "other:v2:1024"

    old = env.holder["providers"]  # type: ignore[attr-defined]
    env.holder["providers"] = Providers(
        OtherEmbedder(), old.reranker, old.chat_model, old.structured_models, old.analyst
    )  # type: ignore[attr-defined]
    events = await env.post("/api/stream/chat", {"question": "What Python work has she done?"})
    name, done = events[-1]
    assert name == "done"
    assert done["refused"] is True
    assert done["answer"] == REFUSAL_TEXT


@pytest.mark.parametrize("question", ["   ", "x" * 2001])
async def test_chat_rejects_invalid_question(env: Env, question: str) -> None:
    resp = await env.client.post("/api/stream/chat", json={"question": question})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
