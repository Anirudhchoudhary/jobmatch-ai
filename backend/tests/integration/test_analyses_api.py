from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.analysis.analyst import Analyst
from app.analysis.heuristic import HeuristicAnalyst
from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.db.engine import get_sessionmaker, make_engine
from app.db.models import Analysis, Chunk, User
from app.ingestion.blobs import LocalBlobStore, get_blob_store
from app.llm.embeddings import HashEmbedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import NoopReranker
from app.llm.structured import StructuredOutputError
from app.main import create_app
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    MatchDraft,
    Requirement,
    RequirementDraft,
    SkillDraft,
    SuggestionDraft,
)
from tests.fixtures.sse import parse_sse
from tests.fixtures.structured import ladder

pytestmark = pytest.mark.integration

Events = list[tuple[str, dict[str, Any]]]
PROSE_JD = (
    "We are a friendly team building logistics software. We value curiosity and kindness, "
    "and we like to ship useful things together."
)


class CountingAnalyst(HeuristicAnalyst):
    def __init__(self) -> None:
        self.calls = 0

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]:
        self.calls += 1
        return await super().extract_requirements(jd)

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]:
        self.calls += 1
        return await super().extract_skills(resume)

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        self.calls += 1
        return await super().match(requirements, skills, resume)


class NoRequirementsAnalyst(CountingAnalyst):
    name = "fake:none"

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]:
        self.calls += 1
        return []


class FailingAnalyst(HeuristicAnalyst):
    name = "fake:failing"

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        raise StructuredOutputError()

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]:  # pragma: no cover
        raise AssertionError("unused")


class Env:
    def __init__(
        self,
        client: httpx.AsyncClient,
        user_id: UUID,
        sm: async_sessionmaker[AsyncSession],
        holder: dict[str, Analyst],
    ) -> None:
        self.client, self.user_id, self.sm, self.holder = client, user_id, sm, holder

    async def stream(self, path: str, body: dict[str, Any] | None = None) -> Events:
        resp = await self.client.post(path, json=body)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        return parse_sse(resp.text)

    async def analyse(self) -> Events:
        return await self.stream("/api/stream/analyses")

    async def samples(self) -> None:
        events = await self.stream("/api/samples")
        assert [d["doc_type"] for n, d in events if n == "done"] == ["resume", "jd"]

    async def ingest_jd(self, jd: str) -> None:
        events = await self.stream("/api/stream/documents", {"doc_type": "jd", "text": jd})
        assert events[-1][0] == "done"

    async def analysis_rows(self) -> int:
        async with self.sm() as s:
            return int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(Analysis)
                        .where(Analysis.user_id == self.user_id)
                    )
                ).scalar_one()
            )


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
    holder: dict[str, Analyst] = {"analyst": HeuristicAnalyst()}

    def providers() -> Providers:
        return Providers(
            HashEmbedder(),
            NoopReranker(),
            GenericFakeChatModel(messages=iter([AIMessage("unused")])),
            ladder(),
            holder["analyst"],
        )

    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.dependency_overrides[get_sessionmaker] = lambda: sm
    app.dependency_overrides[get_blob_store] = lambda: blobs
    app.dependency_overrides[get_providers] = providers
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client, user_id, sm, holder)
    async with sm() as s:
        await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user_id})
        await s.commit()


def _skill(done: dict[str, Any], name: str) -> dict[str, Any]:
    """The requirement whose skill names ``name`` (heuristic skills may keep a prefix)."""
    return next(s for s in done["skills"] if name.lower() in s["requirement"]["skill"].lower())


async def test_analysis_end_to_end_samples(env: Env) -> None:
    await env.samples()
    events = await env.analyse()
    steps = [d["step"] for n, d in events if n == "progress"]
    assert steps == [
        "loading",
        "extracting_requirements",
        "extracting_skills",
        "matching",
        "validating",
        "scoring",
    ]
    name, done = events[-1]
    assert name == "done"
    assert 0 <= done["score"] <= 100
    assert done["cached"] is False
    assert done["analyst"] == HeuristicAnalyst.name
    assert done["request_id"]
    kafka = _skill(done, "Kafka")
    assert kafka["status"] == "matched"
    assert kafka["evidence"]
    assert _skill(done, "Kubernetes")["status"] == "missing"
    async with env.sm() as s:
        resume_ids = set(
            (
                await s.execute(
                    select(Chunk.id).where(Chunk.user_id == env.user_id, Chunk.doc_type == "resume")
                )
            ).scalars()
        )
    assert {UUID(e["chunk_id"]) for e in kafka["evidence"]} <= resume_ids
    assert await env.analysis_rows() == 1


async def test_repeat_analysis_is_cached(env: Env) -> None:
    await env.samples()
    first = (await env.analyse())[-1][1]
    counting = CountingAnalyst()
    env.holder["analyst"] = counting
    events = await env.analyse()
    assert [n for n, _ in events] == ["progress", "done"]
    second = events[-1][1]
    assert second["cached"] is True
    assert second["analysis_id"] == first["analysis_id"]
    assert second["score"] == first["score"]
    assert second["skills"] == first["skills"]
    assert second["request_id"] != first["request_id"]
    assert counting.calls == 0
    assert await env.analysis_rows() == 1


async def test_analysis_requires_both_documents(env: Env) -> None:
    await env.ingest_jd(PROSE_JD)
    events = await env.analyse()
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "documents_required"


async def test_analysis_no_requirements(env: Env) -> None:
    await env.samples()
    await env.ingest_jd(PROSE_JD)
    analyst = NoRequirementsAnalyst()
    env.holder["analyst"] = analyst
    events = await env.analyse()
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "no_requirements"
    assert analyst.calls == 1
    assert await env.analysis_rows() == 0


async def test_analysis_model_failure_emits_error_and_stores_nothing(env: Env) -> None:
    await env.samples()
    env.holder["analyst"] = FailingAnalyst()
    events = await env.analyse()
    name, err = events[-1]
    assert name == "error"
    assert err["code"] == "model_output_invalid"
    assert err["request_id"] and err["request_id"] != "-"
    assert await env.analysis_rows() == 0
