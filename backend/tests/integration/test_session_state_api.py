from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app.analysis.repo import save_analysis
from app.core.settings import get_settings
from app.db.engine import get_engine, get_sessionmaker
from app.db.models import Analysis, Chunk, Document, SuggestionSet, User
from app.main import create_app
from tests.integration.test_analyses_api import Env, engine, env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def _fresh_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


async def _usage_total(env: Env) -> int:  # noqa: F811
    async with env.sm() as s:
        return int(
            (
                await s.execute(
                    text("SELECT coalesce(sum(count), 0) FROM usage_counters WHERE user_id = :i"),
                    {"i": env.user_id},
                )
            ).scalar_one()
        )


async def test_documents_lists_active_docs_only(env: Env) -> None:  # noqa: F811
    assert (await env.client.get("/api/documents")).json() == {"documents": []}
    await env.samples()
    async with env.sm() as s:
        resume_id = (
            await s.execute(
                select(Document.id).where(
                    Document.user_id == env.user_id, Document.doc_type == "resume"
                )
            )
        ).scalar_one()
        chunk = (
            await s.execute(select(Chunk).where(Chunk.doc_id == resume_id).limit(1))
        ).scalar_one()
        chunk.quarantined = True
        s.add(
            Document(
                user_id=env.user_id,
                doc_type="resume",
                source="text",
                content_sha256="f",
                status="failed",
            )
        )
        await s.commit()
    resp = await env.client.get("/api/documents")
    assert resp.status_code == 200
    docs = {d["doc_type"]: d for d in resp.json()["documents"]}
    assert set(docs) == {"resume", "jd"}
    assert docs["resume"]["doc_id"] == str(resume_id)
    assert docs["resume"]["status"] == "ready"
    assert docs["resume"]["source"] == "sample"
    assert docs["resume"]["quarantined_count"] == 1
    assert docs["jd"]["quarantined_count"] == 0
    assert docs["resume"]["chunk_count"] >= 1
    assert set(docs["resume"]) == {
        "doc_id",
        "doc_type",
        "source",
        "filename",
        "status",
        "chunk_count",
        "quarantined_count",
        "created_at",
    }


async def test_latest_analysis_roundtrip_without_allowance(env: Env) -> None:  # noqa: F811
    await env.samples()
    done = (await env.analyse())[-1][1]
    before = await _usage_total(env)
    first = await env.client.get("/api/analyses/latest")
    second = await env.client.get("/api/analyses/latest")
    assert first.status_code == second.status_code == 200
    body = first.json()
    assert body["cached"] is True
    assert body["analysis_id"] == done["analysis_id"]
    assert body["score"] == done["score"]
    assert body["skills"] == done["skills"]
    assert body["request_id"] and body["request_id"] != done["request_id"]
    assert await _usage_total(env) == before
    assert await env.analysis_rows() == 1


async def test_latest_analysis_404_when_none(env: Env) -> None:  # noqa: F811
    resp = await env.client.get("/api/analyses/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "analysis_not_found"
    await env.samples()
    resp = await env.client.get("/api/analyses/latest")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "analysis_not_found"


async def test_latest_suggestions_after_suggest(env: Env) -> None:  # noqa: F811
    await env.samples()
    await env.analyse()
    missing = await env.client.get("/api/suggestions/latest")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "suggestions_not_found"
    posted = (await env.client.post("/api/suggestions", json={})).json()
    before = await _usage_total(env)
    resp = await env.client.get("/api/suggestions/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_id"] == posted["analysis_id"]
    assert body["suggestions"] == posted["suggestions"]
    assert body["rejected_count"] == posted["rejected_count"]
    assert body["analyst"] == posted["analyst"]
    assert body["request_id"] and body["request_id"] != posted["request_id"]
    assert await _usage_total(env) == before


async def test_latest_is_newest_across_analysts(env: Env) -> None:  # noqa: F811
    await env.samples()
    first = (await env.analyse())[-1][1]
    async with env.sm() as s:
        older = (
            await s.execute(select(Analysis).where(Analysis.id == first["analysis_id"]))
        ).scalar_one()
        newer_id = await save_analysis(
            s,
            user_id=env.user_id,
            resume_doc_id=older.resume_doc_id,
            jd_doc_id=older.jd_doc_id,
            analyst="fake:newer",
            result={**older.result, "analyst": "fake:newer"},
        )
    assert str(newer_id) != first["analysis_id"]
    resp = await env.client.get("/api/analyses/latest")
    assert resp.status_code == 200
    assert resp.json()["analysis_id"] == str(newer_id)
    assert resp.json()["analyst"] == "fake:newer"
    assert (await env.client.get("/api/suggestions/latest")).status_code == 404
    posted = (await env.client.post("/api/suggestions", json={})).json()
    assert posted["analysis_id"] == str(newer_id)
    got = await env.client.get("/api/suggestions/latest")
    assert got.status_code == 200
    assert got.json()["analysis_id"] == str(newer_id)


async def test_suggestions_replaced_with_analysis_and_cascade(env: Env) -> None:  # noqa: F811
    await env.samples()
    await env.analyse()
    assert (await env.client.post("/api/suggestions", json={})).status_code == 200
    # A new job description cascades the analysis, and its stored suggestions, away.
    await env.ingest_jd("Requirements\n- Python and Postgres experience\n" * 3)
    assert (await env.client.get("/api/analyses/latest")).status_code == 404
    assert (await env.client.get("/api/suggestions/latest")).status_code == 404
    async with env.sm() as s:
        rows = (
            await s.execute(select(SuggestionSet).where(SuggestionSet.user_id == env.user_id))
        ).all()
    assert rows == []


async def test_chunk_text_owned_only(env: Env) -> None:  # noqa: F811
    await env.samples()
    async with env.sm() as s:
        chunks = (
            (await s.execute(select(Chunk).where(Chunk.user_id == env.user_id).order_by(Chunk.id)))
            .scalars()
            .all()
        )
        mine, hidden = chunks[0], chunks[1]
        hidden.quarantined = True
        other = uuid4()
        s.add(User(id=other))
        await s.flush()
        doc = Document(
            user_id=other, doc_type="resume", source="text", content_sha256="x", status="ready"
        )
        s.add(doc)
        await s.flush()
        foreign = Chunk(
            user_id=other,
            doc_id=doc.id,
            doc_type="resume",
            section="Skills",
            ordinal=0,
            text="foreign secret",
            embedding=[0.0] * 1024,
        )
        s.add(foreign)
        await s.commit()
        mine_id, hidden_id, foreign_id = mine.id, hidden.id, foreign.id
        expected = (mine.doc_type, mine.section, mine.text)
    try:
        ok = await env.client.get(f"/api/chunks/{mine_id}")
        assert ok.status_code == 200
        assert ok.json() == {
            "chunk_id": str(mine_id),
            "doc_type": expected[0],
            "section": expected[1],
            "text": expected[2],
        }
        for cid in (hidden_id, foreign_id, uuid4()):
            resp = await env.client.get(f"/api/chunks/{cid}")
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "chunk_not_found"
        assert (await env.client.get("/api/chunks/not-a-uuid")).status_code == 422
        # A chunk of a replaced (non-active) document is no longer readable.
        async with env.sm() as s:
            await s.execute(
                text(
                    "UPDATE documents SET status = 'failed' "
                    "WHERE id = (SELECT doc_id FROM chunks WHERE id = :c)"
                ),
                {"c": mine_id},
            )
            await s.commit()
        assert (await env.client.get(f"/api/chunks/{mine_id}")).status_code == 404
    finally:
        async with env.sm() as s:
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": other})
            await s.commit()


async def test_read_endpoints_require_session_in_prod(
    monkeypatch: pytest.MonkeyPatch,
    _fresh_settings: None,
) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "k" * 48)
    monkeypatch.setenv("OTP_HMAC_KEY", "o" * 48)
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in (
            "/api/documents",
            "/api/analyses/latest",
            "/api/suggestions/latest",
            f"/api/chunks/{uuid4()}",
        ):
            resp = await client.get(path)
            assert resp.status_code == 401, path
            assert resp.json()["error"]["code"] == "auth_required"
