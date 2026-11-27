from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker, make_engine
from app.db.models import Document, User
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.chat import make_chat_model
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.main import create_app
from tests.fixtures.sse import parse_sse

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


async def test_samples_endpoint_ingests_both(engine: AsyncEngine, tmp_path: Path) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid4()
    async with sm() as s:
        s.add(User(id=user_id))
        await s.commit()
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: LocalBlobStore(tmp_path)
    app.dependency_overrides[get_providers] = lambda: Providers(
        HashEmbedder(), NoopReranker(), make_chat_model(get_settings())
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/samples")
        assert resp.status_code == 200
        done = [d for name, d in parse_sse(resp.text) if name == "done"]
        assert [d["doc_type"] for d in done] == ["resume", "jd"]
        async with sm() as s:
            docs = (
                (await s.execute(select(Document).where(Document.user_id == user_id)))
                .scalars()
                .all()
            )
        assert {(d.doc_type, d.status, d.source) for d in docs} == {
            ("resume", "ready", "sample"),
            ("jd", "ready", "sample"),
        }
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
            await s.commit()
