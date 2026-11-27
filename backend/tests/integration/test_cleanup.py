from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import (
    Chunk,
    Document,
    GuardrailEvent,
    LoginCode,
    RequestLog,
    SemanticCacheEntry,
    UsageCounter,
    User,
)
from app.ops.cleanup import purge_expired

pytestmark = pytest.mark.integration

RETENTION = timedelta(hours=72)
DIM = 1024


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


async def test_purge_removes_only_old_rows(engine: AsyncEngine) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    old, fresh = now - timedelta(hours=73), now - timedelta(hours=1)
    tag = uuid4().hex
    user_id = uuid4()
    old_doc, fresh_doc = uuid4(), uuid4()
    old_cache, expired_cache, fresh_cache = uuid4(), uuid4(), uuid4()
    old_code, fresh_code = uuid4(), uuid4()
    vec = [0.0] * DIM

    async with sm() as s:
        s.add(User(id=user_id))
        await s.flush()
        for doc_id, doc_type, created in ((old_doc, "resume", old), (fresh_doc, "jd", fresh)):
            s.add(
                Document(
                    id=doc_id,
                    user_id=user_id,
                    doc_type=doc_type,
                    source="text",
                    content_sha256=tag,
                    status="ready",
                    created_at=created,
                )
            )
        await s.flush()
        for doc_id, doc_type in ((old_doc, "resume"), (fresh_doc, "jd")):
            s.add(
                Chunk(
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_type=doc_type,
                    section="s",
                    ordinal=0,
                    text="t",
                    embedding=vec,
                )
            )
        for cid, created, expires in (
            (old_cache, old, now + timedelta(hours=1)),  # past retention, unexpired
            (expired_cache, fresh, now - timedelta(minutes=1)),  # expired, recent
            (fresh_cache, fresh, now + timedelta(hours=1)),
        ):
            s.add(
                SemanticCacheEntry(
                    id=cid,
                    user_id=user_id,
                    doc_set_hash=tag,
                    embedding=vec,
                    payload={},
                    created_at=created,
                    expires_at=expires,
                )
            )
        for lid, created in ((old_code, old), (fresh_code, fresh)):
            s.add(
                LoginCode(
                    id=lid,
                    email_hash=tag,
                    ip_hash=tag,
                    code_hash=tag,
                    expires_at=created + timedelta(minutes=10),
                    created_at=created,
                )
            )
        s.add(UsageCounter(user_id=user_id, kind="old", window_start=old, count=1))
        s.add(UsageCounter(user_id=user_id, kind="fresh", window_start=fresh, count=1))
        s.add(
            GuardrailEvent(
                request_id=tag,
                user_id=user_id,
                stage="upload",
                guardrail="g",
                provider="p",
                action="allow",
                mode="enforce",
                reason="r",
                latency_ms=1,
                created_at=old,
            )
        )
        s.add(
            RequestLog(
                request_id=tag,
                user_id=user_id,
                route="/x",
                method="GET",
                status=200,
                latency_ms=1,
                created_at=old,
            )
        )
        await s.commit()

    try:
        counts = await purge_expired(sm, now=now, retention=RETENTION)
        assert counts["documents"] >= 1
        assert counts["semantic_cache"] >= 2
        assert counts["login_codes"] >= 1
        assert counts["usage_counters"] >= 1

        async with sm() as s:
            docs = set(await s.scalars(select(Document.id).where(Document.user_id == user_id)))
            assert docs == {fresh_doc}
            chunk_docs = set(await s.scalars(select(Chunk.doc_id).where(Chunk.user_id == user_id)))
            assert chunk_docs == {fresh_doc}  # old document's chunks cascaded away
            cache = set(
                await s.scalars(
                    select(SemanticCacheEntry.id).where(SemanticCacheEntry.user_id == user_id)
                )
            )
            assert cache == {fresh_cache}
            codes = set(await s.scalars(select(LoginCode.id).where(LoginCode.email_hash == tag)))
            assert codes == {fresh_code}
            kinds = set(
                await s.scalars(select(UsageCounter.kind).where(UsageCounter.user_id == user_id))
            )
            assert kinds == {"fresh"}
            # Non-text rows are kept regardless of age.
            assert await s.get(User, user_id) is not None
            for model in (GuardrailEvent, RequestLog):
                n = await s.scalar(
                    select(func.count()).select_from(model).where(model.user_id == user_id)
                )
                assert n == 1
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
            await s.execute(text("DELETE FROM login_codes WHERE email_hash = :t"), {"t": tag})
            await s.execute(text("DELETE FROM request_log WHERE request_id = :t"), {"t": tag})
            await s.execute(text("DELETE FROM guardrail_events WHERE request_id = :t"), {"t": tag})
            await s.commit()
