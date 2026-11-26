from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.analysis.repo import save_analysis
from app.db.models import Chunk, Document, User
from tests.integration.test_analyses_api import Env, engine, env  # noqa: F401

pytestmark = pytest.mark.integration


async def test_suggestions_end_to_end_samples(env: Env) -> None:  # noqa: F811
    await env.samples()
    await env.analyse()
    resp = await env.client.post("/api/suggestions", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rejected_count"] == 0
    assert body["suggestions"]
    assert len(body["suggestions"]) <= 5
    assert body["request_id"]
    async with env.sm() as s:
        resume_ids = set(
            (
                await s.execute(
                    select(Chunk.id).where(Chunk.user_id == env.user_id, Chunk.doc_type == "resume")
                )
            ).scalars()
        )
    assert {UUID(x["original_chunk_id"]) for x in body["suggestions"]} <= resume_ids


async def test_suggestions_require_analysis(env: Env) -> None:  # noqa: F811
    await env.samples()
    resp = await env.client.post("/api/suggestions", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "analysis_required"


async def test_suggestions_foreign_analysis_id(env: Env) -> None:  # noqa: F811
    await env.samples()
    await env.analyse()
    other = uuid4()
    async with env.sm() as s:
        s.add(User(id=other))
        await s.flush()
        docs = [
            Document(user_id=other, doc_type=t, source="text", content_sha256="x", status="ready")
            for t in ("resume", "jd")
        ]
        s.add_all(docs)
        await s.commit()
        foreign = await save_analysis(
            s,
            user_id=other,
            resume_doc_id=docs[0].id,
            jd_doc_id=docs[1].id,
            analyst="x",
            result={"skills": []},
        )
    for aid in (foreign, uuid4()):
        resp = await env.client.post("/api/suggestions", json={"analysis_id": str(aid)})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "analysis_not_found"
    async with env.sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": other})
        await s.commit()


async def test_suggestions_stale_analysis_after_replacement(env: Env) -> None:  # noqa: F811
    await env.samples()
    await env.analyse()
    # Replacing the job description cascades the analysis away.
    await env.ingest_jd("Requirements\n- Python and Postgres experience\n" * 3)
    resp = await env.client.post("/api/suggestions", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "analysis_required"
