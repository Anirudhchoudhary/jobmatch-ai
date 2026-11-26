from uuid import uuid4

import pytest

from app.analysis.grounding import validate_suggestions
from app.analysis.heuristic import HeuristicAnalyst
from app.api.samples import SAMPLES_DIR as SAMPLES
from app.graphs.citations import label_context
from app.ingestion.chunking import ChunkDraft, chunk_jd, chunk_resume
from app.retrieval.search import RetrievedChunk
from app.schemas.match import RequirementDraft


def retrieved(doc_type: str, drafts: list[ChunkDraft]) -> list[RetrievedChunk]:
    doc_id = uuid4()
    return [
        RetrievedChunk(uuid4(), doc_id, doc_type, d.section, d.ordinal, d.text, 1.0) for d in drafts
    ]


@pytest.fixture
def resume() -> dict[str, RetrievedChunk]:
    text = (SAMPLES / "resume.md").read_text(encoding="utf-8")
    return label_context(retrieved("resume", chunk_resume(text)))


@pytest.fixture
def jd() -> dict[str, RetrievedChunk]:
    text = (SAMPLES / "jd.md").read_text(encoding="utf-8")
    return label_context(retrieved("jd", chunk_jd(text)))


async def test_heuristic_requirements_from_sections(jd: dict[str, RetrievedChunk]) -> None:
    reqs = await HeuristicAnalyst().extract_requirements(jd)
    kube = [r for r in reqs if "Kubernetes" in r.skill]
    assert len(kube) == 1
    assert kube[0].importance == "required"
    assert kube[0].text == "Production experience with Kubernetes."
    assert jd[kube[0].source].section == "Requirements"
    nice = [r for r in reqs if jd[r.source].section == "Nice to have"]
    assert len(nice) == 3
    assert all(r.importance == "nice_to_have" for r in nice)
    assert all(jd[r.source].section in {"Requirements", "Nice to have"} for r in reqs)
    assert len(reqs) == 8
    terraform = next(r for r in reqs if "Terraform" in r.text)
    assert terraform.skill == "Experience with infrastructure as code"


async def test_heuristic_requirements_fallback_skips_about_and_benefits() -> None:
    chunks = [
        RetrievedChunk(uuid4(), uuid4(), "jd", "About", 0, "We are great.", 1.0),
        RetrievedChunk(uuid4(), uuid4(), "jd", "Responsibilities", 1, "- Build APIs (REST)", 1.0),
        RetrievedChunk(uuid4(), uuid4(), "jd", "Benefits", 2, "Free lunch", 1.0),
    ]
    reqs = await HeuristicAnalyst().extract_requirements(label_context(chunks))
    assert [(r.skill, r.text, r.importance, r.source) for r in reqs] == [
        ("Build APIs", "Build APIs (REST)", "required", "j2")
    ]


async def test_heuristic_match_kafka_matched_kubernetes_missing(
    resume: dict[str, RetrievedChunk],
) -> None:
    reqs = [
        RequirementDraft(skill="Kafka", text="Kafka", importance="required", source="j1"),
        RequirementDraft(skill="Kubernetes", text="K8s", importance="required", source="j1"),
        RequirementDraft(skill="Kafka Streams", text="t", importance="nice_to_have", source="j1"),
    ]
    matches = await HeuristicAnalyst().match(reqs, [], resume)
    assert [(m.requirement_index, m.status) for m in matches] == [
        (0, "matched"),
        (1, "missing"),
        (2, "partial"),
    ]
    ev = matches[0].evidence[0]
    assert resume[ev.source].doc_type == "resume"
    assert "Kafka" in ev.quote
    assert ev.quote in resume[ev.source].text
    assert matches[1].evidence == []


async def test_heuristic_extract_skills_empty(resume: dict[str, RetrievedChunk]) -> None:
    assert await HeuristicAnalyst().extract_skills(resume) == []


async def test_heuristic_rewrite_is_guard_clean(resume: dict[str, RetrievedChunk]) -> None:
    drafts = await HeuristicAnalyst().rewrite(resume, [])
    assert len(drafts) == len(resume)
    assert all(d.rewritten == d.original for d in drafts)
    assert drafts[0].rationale == "Offline mode: rewriting needs a real model."
    kept, rejected = validate_suggestions(drafts, resume)
    assert rejected == 0
    assert len(kept) == len(drafts)


def test_heuristic_name() -> None:
    assert HeuristicAnalyst().name == "heuristic:v1"
