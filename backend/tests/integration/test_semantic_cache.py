import itertools
import math
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis.heuristic import HeuristicAnalyst
from app.api.deps import get_current_user_id
from app.cache.semantic import CachedAnswer, SemanticCache
from app.core.settings import get_settings
from app.db.engine import get_engine, get_sessionmaker, make_engine
from app.db.models import RequestLog, SemanticCacheEntry, User
from app.guardrails.factory import build_guardrails, get_guardrails
from app.guardrails.recorder import DbRecorder
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.documents import JD
from tests.fixtures.sse import parse_sse
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration

QUESTION = "What Python work has she done?"
DIM = 1024


def unit(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


ANSWER = CachedAnswer(answer="Python [r1]", citations=[], invalid_citations=[])


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def users(engine: AsyncEngine) -> AsyncIterator[list[UUID]]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids = [uuid4(), uuid4()]
    async with sm() as s:
        s.add_all(User(id=i) for i in ids)
        await s.commit()
    yield ids
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})
        await s.commit()


class Client:
    def __init__(self, client: httpx.AsyncClient, sm: async_sessionmaker[AsyncSession]) -> None:
        self.client, self.sm = client, sm
        self.request_ids: list[str] = []

    async def post(self, path: str, body: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
        request_id = f"semcache-{uuid4().hex[:12]}"
        self.request_ids.append(request_id)
        resp = await self.client.post(path, json=body, headers={"X-Request-ID": request_id})
        assert resp.status_code == 200, resp.text
        return parse_sse(resp.text)

    async def chat(self) -> dict[str, object]:
        events = await self.post("/api/stream/chat", {"question": QUESTION})
        name, done = events[-1]
        assert name == "done", events
        return done

    async def ingest_jd(self, jd: str) -> None:
        events = await self.post("/api/stream/documents", {"doc_type": "jd", "text": jd})
        assert events[-1][0] == "done", events


@pytest.fixture
async def api(engine: AsyncEngine, users: list[UUID], tmp_path: Path) -> AsyncIterator[Client]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # The request-log middleware resolves the cached app sessionmaker; start from a clean
    # cache so it binds to this test's event loop.
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    app = create_app()
    blobs = LocalBlobStore(tmp_path)
    model = GenericFakeChatModel(messages=itertools.repeat(AIMessage("Python [r1]")))
    providers = Providers(HashEmbedder(), NoopReranker(), model, ladder(), HeuristicAnalyst())
    app.dependency_overrides[get_current_user_id] = lambda: users[0]
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: blobs
    app.dependency_overrides[get_guardrails] = lambda: build_guardrails(
        get_settings(), DbRecorder(sm), models=None, bedrock_client=None
    )
    app.dependency_overrides[get_providers] = lambda: providers
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            c = Client(client, sm)
            yield c
    finally:
        async with sm() as s:
            await s.execute(
                text("DELETE FROM request_log WHERE request_id = ANY(:r)"), {"r": c.request_ids}
            )
            await s.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


async def _cache_hit_flag(sm: async_sessionmaker[AsyncSession], request_id: str) -> bool:
    async with sm() as s:
        return (
            await s.execute(select(RequestLog.cache_hit).where(RequestLog.request_id == request_id))
        ).scalar_one()


async def test_same_question_hits_second_time(api: Client) -> None:
    await api.ingest_jd(JD)
    first = await api.chat()
    assert first["cached"] is False and first["refused"] is False
    second = await api.chat()
    assert second["cached"] is True
    assert second["answer"] == first["answer"]
    assert second["citations"] == first["citations"]
    assert await _cache_hit_flag(api.sm, api.request_ids[-2]) is False
    assert await _cache_hit_flag(api.sm, api.request_ids[-1]) is True


async def test_new_upload_invalidates(api: Client, users: list[UUID]) -> None:
    await api.ingest_jd(JD)
    assert (await api.chat())["cached"] is False
    assert (await api.chat())["cached"] is True
    other = "We need a senior Python engineer to own Postgres data pipelines and APIs. " * 3
    await api.ingest_jd(other)
    async with api.sm() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(SemanticCacheEntry)
                .where(SemanticCacheEntry.user_id == users[0])
            )
        ).scalar_one()
    assert count == 0
    assert (await api.chat())["cached"] is False


async def test_users_do_not_share_entries(engine: AsyncEngine, users: list[UUID]) -> None:
    cache = SemanticCache(
        async_sessionmaker(engine, expire_on_commit=False), threshold=0.95, ttl=timedelta(hours=1)
    )
    a, b = users
    await cache.store(a, "key", unit(0), ANSWER)
    assert await cache.lookup(a, "key", unit(0)) == ANSWER
    assert await cache.lookup(b, "key", unit(0)) is None


async def test_other_doc_set_misses(engine: AsyncEngine, users: list[UUID]) -> None:
    cache = SemanticCache(
        async_sessionmaker(engine, expire_on_commit=False), threshold=0.95, ttl=timedelta(hours=1)
    )
    await cache.store(users[0], "key", unit(0), ANSWER)
    assert await cache.lookup(users[0], "other-key", unit(0)) is None


async def test_expired_entry_misses(engine: AsyncEngine, users: list[UUID]) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            SemanticCacheEntry(
                user_id=users[0],
                doc_set_hash="key",
                embedding=unit(0),
                payload=ANSWER.model_dump(mode="json"),
                expires_at=func.now() - timedelta(minutes=1),
            )
        )
        await s.commit()
    cache = SemanticCache(sm, threshold=0.95, ttl=timedelta(hours=1))
    assert await cache.lookup(users[0], "key", unit(0)) is None


async def test_threshold_respected(engine: AsyncEngine, users: list[UUID]) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    near = [0.0] * DIM
    near[0], near[1] = 0.9, math.sqrt(1 - 0.9**2)  # cosine 0.9 with unit(0)
    strict = SemanticCache(sm, threshold=0.95, ttl=timedelta(hours=1))
    await strict.store(users[0], "key", unit(0), ANSWER)
    assert await strict.lookup(users[0], "key", near) is None
    loose = SemanticCache(sm, threshold=0.85, ttl=timedelta(hours=1))
    assert await loose.lookup(users[0], "key", near) == ANSWER
