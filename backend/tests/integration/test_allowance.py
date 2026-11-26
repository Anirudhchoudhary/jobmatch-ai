import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.analysis.heuristic import HeuristicAnalyst
from app.api.deps import get_current_user_id
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker, make_engine
from app.db.models import User
from app.guardrails.allowance import QuotaExceeded, RateLimited, consume
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.documents import JD
from tests.fixtures.sse import parse_sse
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
async def user_id(engine: AsyncEngine) -> AsyncIterator[UUID]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid4()
    async with sm() as s:
        s.add(User(id=uid))
        await s.commit()
    yield uid
    async with sm() as s:
        await s.execute(text("DELETE FROM guardrail_events WHERE user_id = :i"), {"i": uid})
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        await s.commit()


@pytest.fixture
async def client(
    engine: AsyncEngine, user_id: UUID, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app()
    providers = Providers(
        HashEmbedder(),
        NoopReranker(),
        GenericFakeChatModel(messages=iter(AIMessage(f"Kafka [r1] {i}") for i in range(50))),
        ladder(),
        HeuristicAnalyst(),
    )
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: LocalBlobStore(tmp_path)
    app.dependency_overrides[get_providers] = lambda: providers
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _settings(monkeypatch: pytest.MonkeyPatch, **env: int) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key.upper(), str(value))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def _chat(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/stream/chat", json={"question": "What work has she done?"})


async def test_daily_limit(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    user_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, allowance_chats=2)
    assert (await _chat(client)).status_code == 200
    assert (await _chat(client)).status_code == 200
    third = await _chat(client)
    assert third.status_code == 429
    error = third.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert "2 chats" in error["message"]
    async with async_sessionmaker(engine)() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT reason, action FROM guardrail_events "
                    "WHERE user_id = :i AND guardrail = 'allowance'"
                ),
                {"i": user_id},
            )
        ).all()
    assert [(r.reason, r.action) for r in rows] == [("quota:chats", "block")]


async def test_burst_limit(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, burst_per_minute=1)
    assert (await _chat(client)).status_code == 200
    second = await _chat(client)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


async def test_day_rollover(engine: AsyncEngine, user_id: UUID) -> None:
    settings = Settings(allowance_chats=1, burst_per_minute=100)  # type: ignore[call-arg]
    sm = async_sessionmaker(engine, expire_on_commit=False)
    late = datetime(2031, 3, 4, 23, 59, tzinfo=UTC)
    early = datetime(2031, 3, 5, 0, 0, tzinfo=UTC)
    async with sm() as s:
        await consume(s, user_id, "chats", settings=settings, now=late)
    async with sm() as s:
        with pytest.raises(QuotaExceeded):
            await consume(s, user_id, "chats", settings=settings, now=late)
    async with sm() as s:
        await consume(s, user_id, "chats", settings=settings, now=early)


async def test_concurrent_consume_is_atomic(engine: AsyncEngine, user_id: UUID) -> None:
    settings = Settings(allowance_chats=1, burst_per_minute=100)  # type: ignore[call-arg]
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def attempt() -> None:
        async with sm() as s:
            await consume(s, user_id, "chats", settings=settings)

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    refused = [r for r in results if isinstance(r, QuotaExceeded)]
    assert len(refused) == 1
    assert sum(r is None for r in results) == 1


async def test_burst_checked_before_daily(engine: AsyncEngine, user_id: UUID) -> None:
    settings = Settings(allowance_chats=1, burst_per_minute=1)  # type: ignore[call-arg]
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await consume(s, user_id, "chats", settings=settings)
    async with sm() as s:
        with pytest.raises(RateLimited):
            await consume(s, user_id, "chats", settings=settings)


async def test_cached_analysis_is_free(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, allowance_analyses=1)
    assert (await client.post("/api/samples")).status_code == 200
    first = await client.post("/api/stream/analyses")
    second = await client.post("/api/stream/analyses")
    assert first.status_code == second.status_code == 200
    assert parse_sse(first.text)[-1][1]["cached"] is False
    events = parse_sse(second.text)
    assert events[-1][0] == "done"
    assert events[-1][1]["cached"] is True


async def test_invalid_request_is_not_charged(
    client: httpx.AsyncClient, engine: AsyncEngine, user_id: UUID
) -> None:
    resp = await client.post("/api/stream/chat", json={"question": "   "})
    assert resp.status_code == 422
    async with async_sessionmaker(engine)() as s:
        n = (
            await s.execute(
                text("SELECT count(*) FROM usage_counters WHERE user_id = :i"), {"i": user_id}
            )
        ).scalar_one()
    assert n == 0


async def test_fresh_analysis_over_limit_is_an_sse_error(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, allowance_analyses=1)
    assert (await client.post("/api/samples")).status_code == 200
    first = await client.post("/api/stream/analyses")
    assert parse_sse(first.text)[-1][0] == "done"
    changed = await client.post(
        "/api/stream/documents", json={"doc_type": "jd", "text": JD + "\n- Experience with Rust."}
    )
    assert changed.status_code == 200
    second = await client.post("/api/stream/analyses")
    name, data = parse_sse(second.text)[-1]
    assert name == "error"
    assert data["code"] == "quota_exceeded"
