from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.analysis.heuristic import HeuristicAnalyst
from app.api.deps import DEV_USER_ID
from app.core.settings import get_settings
from app.db.engine import get_engine, get_sessionmaker, make_engine
from app.db.models import RequestLog
from app.guardrails.factory import build_guardrails, get_guardrails
from app.guardrails.recorder import DbRecorder
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


async def test_chat_request_writes_row(engine: AsyncEngine, tmp_path: Path) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # The middleware's writer and the real get_current_user_id resolve the cached app
    # sessionmaker; start from a clean cache so it binds to this test's event loop.
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    app = create_app()
    providers = Providers(
        HashEmbedder(),
        NoopReranker(),
        GenericFakeChatModel(messages=iter([AIMessage("x")])),
        ladder(),
        HeuristicAnalyst(),
    )
    app.dependency_overrides[get_blob_store] = lambda: LocalBlobStore(tmp_path)
    app.dependency_overrides[get_guardrails] = lambda: build_guardrails(
        get_settings(), DbRecorder(sm), models=None, bedrock_client=None
    )
    app.dependency_overrides[get_providers] = lambda: providers
    transport = httpx.ASGITransport(app=app)
    request_id = "reqlog-test-0001"
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/stream/chat",
                json={"question": "What Python work has she done?"},
                headers={"X-Request-ID": request_id},
            )
        assert resp.status_code == 200
        async with sm() as s:
            rows = (
                (await s.execute(select(RequestLog).where(RequestLog.request_id == request_id)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.route == "/api/stream/chat" and row.method == "POST"
        assert row.status == 200
        assert row.user_id == DEV_USER_ID
        assert row.latency_ms >= 0
    finally:
        async with sm() as s:
            await s.execute(
                text("DELETE FROM request_log WHERE request_id = :r"), {"r": request_id}
            )
            await s.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
