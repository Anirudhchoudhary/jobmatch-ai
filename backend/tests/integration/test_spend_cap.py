from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from app.db.models import RequestLog, User
from app.guardrails.allowance import consume_or_refuse
from app.guardrails.spend import DemoAtCapacity
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration

_DELETE_TODAY = text(
    "DELETE FROM request_log "
    "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


@pytest.fixture
def sm(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def user_id(sm: async_sessionmaker) -> AsyncIterator[UUID]:  # type: ignore[type-arg]
    uid = uuid4()
    async with sm() as s:
        s.add(User(id=uid))
        await s.execute(_DELETE_TODAY)
        await s.commit()
    yield uid
    async with sm() as s:
        await s.execute(text("DELETE FROM guardrail_events WHERE user_id = :i"), {"i": uid})
        await s.execute(text("DELETE FROM usage_counters WHERE user_id = :i"), {"i": uid})
        await s.execute(_DELETE_TODAY)
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})
        await s.commit()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def _spend(
    sm: async_sessionmaker,  # type: ignore[type-arg]
    *costs: str,
    created_at: datetime | None = None,
) -> None:
    async with sm() as s:
        for cost in costs:
            row = RequestLog(
                request_id=str(uuid4()),
                route="/api/test",
                method="POST",
                status=200,
                latency_ms=1,
                cost_usd=Decimal(cost),
            )
            if created_at is not None:
                row.created_at = created_at
            s.add(row)
        await s.commit()


def _settings(cap: float | None) -> Settings:
    return Settings(daily_spend_cap_usd=cap, burst_per_minute=100)  # type: ignore[call-arg]


async def test_under_cap_allows(sm: async_sessionmaker, user_id: UUID) -> None:  # type: ignore[type-arg]
    await _spend(sm, "0.30", "0.20")
    await consume_or_refuse(sm, user_id, "chats", settings=_settings(1.0))


async def test_at_cap_refuses_with_message_and_event(
    sm: async_sessionmaker,  # type: ignore[type-arg]
    user_id: UUID,
) -> None:
    await _spend(sm, "0.60", "0.40")
    with pytest.raises(DemoAtCapacity) as exc:
        await consume_or_refuse(sm, user_id, "chats", settings=_settings(1.0))
    assert exc.value.status == 503
    assert exc.value.message == "The demo is at capacity today. Please try again tomorrow."
    async with sm() as s:
        events = (
            await s.execute(
                text(
                    "SELECT reason, action FROM guardrail_events "
                    "WHERE user_id = :i AND guardrail = 'spend_cap'"
                ),
                {"i": user_id},
            )
        ).all()
        counters = (
            await s.execute(
                text("SELECT count(*) FROM usage_counters WHERE user_id = :i"), {"i": user_id}
            )
        ).scalar_one()
    assert [(e.reason, e.action) for e in events] == [("spend_cap", "block")]
    assert counters == 0


async def test_yesterdays_spend_ignored(sm: async_sessionmaker, user_id: UUID) -> None:  # type: ignore[type-arg]
    yesterday = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        minutes=1
    )
    await _spend(sm, "5.00", created_at=yesterday)
    await consume_or_refuse(sm, user_id, "chats", settings=_settings(1.0))


async def test_cap_none_disables(sm: async_sessionmaker, user_id: UUID) -> None:  # type: ignore[type-arg]
    await _spend(sm, "100.00")
    await consume_or_refuse(sm, user_id, "chats", settings=_settings(None))


async def test_chat_api_at_capacity_returns_503(
    sm: async_sessionmaker,  # type: ignore[type-arg]
    user_id: UUID,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "0")
    get_settings.cache_clear()
    app = create_app()
    providers = Providers(
        HashEmbedder(),
        NoopReranker(),
        GenericFakeChatModel(messages=iter(AIMessage("Kafka [r1]") for _ in range(5))),
        ladder(),
        HeuristicAnalyst(),
    )
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: LocalBlobStore(tmp_path)
    app.dependency_overrides[get_providers] = lambda: providers
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/stream/chat", json={"question": "What work has she done?"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "demo_at_capacity"
