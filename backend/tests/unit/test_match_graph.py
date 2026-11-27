import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from app.analysis.repo import LoadedDocuments
from app.api.sse import sse_stream
from app.core.errors import ProviderError
from app.graphs import match as match_module
from app.graphs.match import analysis_events
from app.llm.structured import StructuredOutputError
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    AnalysisProgress,
    EvidenceDraft,
    MatchAnalysis,
    MatchDraft,
    Requirement,
    RequirementDraft,
    SkillDraft,
    SuggestionDraft,
)

RESUME_DOC, JD_DOC = uuid4(), uuid4()
RESUME_CHUNK = RetrievedChunk(
    uuid4(), RESUME_DOC, "resume", "Experience", 0, "ZQXTEXT Built Kafka pipelines.", 0.0
)
JD_CHUNK = RetrievedChunk(
    uuid4(), JD_DOC, "jd", "Requirements", 0, "- Kafka\n- Kubernetes\n- AWS", 0.0
)
DOCS = LoadedDocuments(RESUME_DOC, JD_DOC, [RESUME_CHUNK], [JD_CHUNK])

REQUIREMENTS = [
    RequirementDraft(skill="Kafka", text="Kafka", importance="required", source="j1"),
    RequirementDraft(skill="Kubernetes", text="Kubernetes", importance="required", source="j1"),
    RequirementDraft(skill="AWS", text="AWS", importance="nice_to_have", source="j1"),
]
GOOD_QUOTE = "built kafka pipelines"


class FakeAnalyst:
    name = "fake:v1"

    def __init__(self, requirements: list[RequirementDraft], kafka_quote: str = GOOD_QUOTE) -> None:
        self.requirements = requirements
        self.kafka_quote = kafka_quote
        self.calls: dict[str, int] = {"requirements": 0, "skills": 0, "match": 0, "rewrite": 0}
        self.seen: dict[str, Any] = {}

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]:
        self.calls["requirements"] += 1
        self.seen["jd"] = dict(jd)
        return list(self.requirements)

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]:
        self.calls["skills"] += 1
        self.seen["resume"] = dict(resume)
        return [SkillDraft(skill="Kafka", evidence=[])]

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        self.calls["match"] += 1
        self.seen["match_requirements"] = list(requirements)
        return [
            MatchDraft(
                requirement_index=0,
                status="matched",
                evidence=[EvidenceDraft(source="r1", quote=self.kafka_quote)],
            ),
            MatchDraft(requirement_index=1, status="missing", evidence=[]),
            MatchDraft(
                requirement_index=2,
                status="partial",
                evidence=[EvidenceDraft(source="r1", quote="Kafka pipelines")],
            ),
        ]

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]:  # pragma: no cover
        raise AssertionError("rewrite is not part of the match graph")


class FakeSession:
    pass


@asynccontextmanager
async def _session() -> AsyncIterator[FakeSession]:
    yield FakeSession()


def fake_sessionmaker() -> Any:
    return _session()


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[str, Any] = {"saved": [], "cached": None}
    saved_id = uuid4()

    async def load_documents(session: object, user_id: UUID) -> LoadedDocuments:
        return DOCS

    async def get_cached_analysis(
        session: object, user_id: UUID, resume_doc_id: UUID, jd_doc_id: UUID, analyst: str
    ) -> tuple[UUID, dict[str, Any]] | None:
        return store["cached"]  # type: ignore[no-any-return]

    async def save_analysis(session: object, **kwargs: Any) -> UUID:
        store["saved"].append(kwargs)
        return saved_id

    monkeypatch.setattr(match_module, "load_documents", load_documents)
    monkeypatch.setattr(match_module, "get_cached_analysis", get_cached_analysis)
    monkeypatch.setattr(match_module, "save_analysis", save_analysis)
    store["saved_id"] = saved_id
    return store


async def run(analyst: FakeAnalyst) -> list[tuple[str, Any]]:
    return [
        ev
        async for ev in analysis_events(
            user_id=uuid4(), sessionmaker=fake_sessionmaker, analyst=analyst
        )
    ]


async def run_sse(analyst: FakeAnalyst) -> list[tuple[str, dict[str, Any]]]:
    out = []
    events = analysis_events(user_id=uuid4(), sessionmaker=fake_sessionmaker, analyst=analyst)
    async for raw in sse_stream(events):
        head, body = raw.decode().split("\ndata: ", 1)
        out.append((head.removeprefix("event: "), json.loads(body)))
    return out


async def test_events_and_score(repo: dict[str, Any]) -> None:
    analyst = FakeAnalyst(REQUIREMENTS)
    with capture_logs() as logs:
        events = await run(analyst)
    steps = [e.step for n, e in events if isinstance(e, AnalysisProgress)]
    assert steps == [
        "loading",
        "extracting_requirements",
        "extracting_skills",
        "matching",
        "validating",
        "scoring",
    ]
    assert [n for n, _ in events] == ["progress"] * 6 + ["done"]
    done = events[-1][1]
    assert isinstance(done, MatchAnalysis)
    # Kafka matched (3*1) + Kubernetes missing (3*0) + AWS partial (1*0.5) = 3.5 / 7 -> 50.
    assert done.score == 50
    assert done.required_coverage == 0.5
    assert done.nice_to_have_coverage == 0.5
    assert [s.status for s in done.skills] == ["matched", "missing", "partial"]
    assert done.skills[0].evidence[0].chunk_id == RESUME_CHUNK.chunk_id
    assert {s.requirement.jd_chunk_id for s in done.skills} == {JD_CHUNK.chunk_id}
    assert done.downgraded_count == 0
    assert done.cached is False
    assert done.analyst == "fake:v1"
    assert done.analysis_id == repo["saved_id"]
    # Analyst sees per-document labelled mappings.
    assert set(analyst.seen["jd"]) == {"j1"}
    assert set(analyst.seen["resume"]) == {"r1"}
    # Saved result has no per-request fields and rebuilds the same analysis.
    (saved,) = repo["saved"]
    assert saved["analyst"] == "fake:v1"
    assert saved["resume_doc_id"] == RESUME_DOC and saved["jd_doc_id"] == JD_DOC
    result = saved["result"]
    assert not {"analysis_id", "cached", "request_id"} & set(result)
    rebuilt = MatchAnalysis(
        **result, analysis_id=done.analysis_id, cached=False, request_id=done.request_id
    )
    assert rebuilt == done
    # Logs carry no document text.
    assert "ZQXTEXT" not in repr(logs)
    completed = next(e for e in logs if e["event"] == "analysis_completed")
    assert completed["score"] == 50 and completed["cached"] is False


async def test_analysis_downgrades_fabricated_evidence(repo: dict[str, Any]) -> None:
    events = await run(FakeAnalyst(REQUIREMENTS, kafka_quote="led a Kafka migration at Initech"))
    done = events[-1][1]
    assert isinstance(done, MatchAnalysis)
    assert done.downgraded_count == 1
    assert done.skills[0].status == "missing"
    assert done.skills[0].evidence == []
    assert done.score < 50


async def test_no_requirements_stops_before_match(repo: dict[str, Any]) -> None:
    analyst = FakeAnalyst([])
    out = await run_sse(analyst)
    assert out[-1][0] == "error"
    assert out[-1][1]["code"] == "no_requirements"
    assert analyst.calls["match"] == 0
    assert analyst.calls["skills"] == 0
    assert repo["saved"] == []


async def test_requirements_citing_non_jd_labels_are_dropped(repo: dict[str, Any]) -> None:
    bad = RequirementDraft(skill="Kafka", text="Kafka", importance="required", source="r1")
    analyst = FakeAnalyst([bad])
    out = await run_sse(analyst)
    assert out[-1][1]["code"] == "no_requirements"
    assert analyst.calls["match"] == 0


async def test_cache_hit_skips_analyst(repo: dict[str, Any]) -> None:
    first = await run(FakeAnalyst(REQUIREMENTS))
    done = first[-1][1]
    repo["cached"] = (done.analysis_id, repo["saved"][0]["result"])
    analyst = FakeAnalyst(REQUIREMENTS)
    events = await run(analyst)
    assert [n for n, _ in events] == ["progress", "done"]
    hit = events[-1][1]
    assert isinstance(hit, MatchAnalysis)
    assert hit.cached is True
    assert hit.analysis_id == done.analysis_id
    assert hit.score == done.score
    assert sum(analyst.calls.values()) == 0
    assert len(repo["saved"]) == 1


class SkillsFailAnalyst(FakeAnalyst):
    def __init__(self, error: Exception) -> None:
        super().__init__(REQUIREMENTS)
        self.error = error

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]:
        self.calls["skills"] += 1
        raise self.error

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        self.seen["match_skills"] = list(skills)
        return await super().match(requirements, skills, resume)


async def test_skills_extraction_failure_is_not_fatal(repo: dict[str, Any]) -> None:
    analyst = SkillsFailAnalyst(StructuredOutputError())
    with capture_logs() as logs:
        events = await run(analyst)
    done = events[-1][1]
    assert isinstance(done, MatchAnalysis)
    assert done.score == 50
    assert analyst.seen["match_skills"] == []
    assert any(e["event"] == "skills_extraction_failed" for e in logs)
    assert len(repo["saved"]) == 1


async def test_skills_extraction_provider_error_is_fatal(repo: dict[str, Any]) -> None:
    out = await run_sse(SkillsFailAnalyst(ProviderError("anthropic")))
    assert out[-1][0] == "error"
    assert out[-1][1]["code"] == "provider_unavailable"
    assert repo["saved"] == []
