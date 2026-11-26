from collections.abc import Mapping, Sequence
from uuid import uuid4

from app.analysis.heuristic import HeuristicAnalyst
from app.graphs.suggest import build_suggest_graph, initial_state, select_relevant_bullets
from app.retrieval.search import RetrievedChunk
from app.schemas.match import Requirement, SkillMatch, SuggestionDraft


def chunk(section: str, ordinal: int, text: str) -> RetrievedChunk:
    return RetrievedChunk(uuid4(), uuid4(), "resume", section, ordinal, text, 0.0)


def req(skill: str, importance: str = "required") -> Requirement:
    return Requirement.model_validate(
        {"skill": skill, "text": skill, "importance": importance, "jd_chunk_id": uuid4()}
    )


def match(skill: str, status: str = "matched") -> SkillMatch:
    return SkillMatch(requirement=req(skill), status=status, evidence=[])  # type: ignore[arg-type]


class FakeAnalyst(HeuristicAnalyst):
    name = "fake:suggest"

    def __init__(self, drafts: list[SuggestionDraft]) -> None:
        self.drafts = drafts
        self.calls = 0
        self.labels: list[str] = []

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]:
        self.calls += 1
        self.labels = list(bullets)
        return self.drafts


def test_select_relevant_bullets_ranks_and_limits() -> None:
    two = chunk("Experience", 1, "Built Kafka and Python services")
    zero = chunk("Experience", 2, "Organised team lunches")
    one = chunk("Projects", 3, "Wrote a Kafka consumer")
    skills = chunk("Skills", 0, "Kafka Python")
    edu = chunk("Education", 4, "Kafka Python course")
    out = select_relevant_bullets(
        [skills, two, zero, one, edu], [req("Kafka"), req("Python")], limit=5
    )
    assert out == [two, one]
    assert select_relevant_bullets([skills, two, zero, one, edu], [req("Kafka")], limit=1) == [two]


async def test_suggestions_reject_fabricated_rewrite() -> None:
    src = chunk("Experience", 0, "Built Kafka pipelines for the data team")
    ok = SuggestionDraft(
        source="r1",
        original="Built Kafka pipelines",
        rewritten="Built Kafka pipelines for the data team",
        rationale="Clearer",
        target_skills=["Kafka"],
    )
    bad = SuggestionDraft(
        source="r1",
        original="Built Kafka pipelines",
        rewritten="Built Kafka pipelines at Google cutting latency 40% on Kubernetes",
        rationale="Impact",
        target_skills=["Kubernetes"],
    )
    analyst = FakeAnalyst([ok, bad])
    graph = build_suggest_graph(analyst, limit=5)
    final = await graph.ainvoke(initial_state([src], [match("Kafka")]))
    assert len(final["suggestions"]) == 1
    assert final["suggestions"][0].original_chunk_id == src.chunk_id
    assert final["rejected"] == 1
    assert analyst.labels == ["r1"]


async def test_no_bullets_skips_rewrite() -> None:
    analyst = FakeAnalyst([])
    graph = build_suggest_graph(analyst, limit=5)
    final = await graph.ainvoke(initial_state([chunk("Skills", 0, "Kafka")], [match("Kafka")]))
    assert analyst.calls == 0
    assert final["suggestions"] == []
    assert final["rejected"] == 0


async def test_suggestions_reject_lowercase_target_skill() -> None:
    src = chunk("Experience", 0, "Built Kafka pipelines for the data team")
    bad = SuggestionDraft(
        source="r1",
        original="Built Kafka pipelines",
        rewritten="Built Kafka pipelines on kubernetes for the data team",
        rationale="Targets Kubernetes",
        target_skills=["Kubernetes"],
    )
    graph = build_suggest_graph(FakeAnalyst([bad]), limit=5)
    skills = [match("Kafka"), match("Kubernetes", "partial")]
    final = await graph.ainvoke(initial_state([src], skills))
    assert (final["suggestions"], final["rejected"]) == ([], 1)
