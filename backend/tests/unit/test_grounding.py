from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analysis.grounding import normalize, quote_in_chunk, validate_matches
from app.retrieval.search import RetrievedChunk
from app.schemas.match import EvidenceDraft, MatchDraft, Requirement

R1 = RetrievedChunk(
    uuid4(), uuid4(), "resume", "Exp", 0, "Built Kafka   pipelines\nprocessing 2M events/day", 0.9
)
J1 = RetrievedChunk(uuid4(), uuid4(), "jd", "Req", 0, "Needs Kafka pipelines", 0.9)
LABELLED = {"r1": R1, "j1": J1}


def req(skill: str = "Kafka") -> Requirement:
    return Requirement(skill=skill, text="t", importance="required", jd_chunk_id=J1.chunk_id)


def draft(idx: int, status: str, source: str, quote: str) -> MatchDraft:
    return MatchDraft.model_validate(
        {
            "requirement_index": idx,
            "status": status,
            "evidence": [{"source": source, "quote": quote}],
        }
    )


def test_normalize() -> None:
    assert normalize("  Ａ  b\n\tC ") == "a b c"


def test_quote_normalization() -> None:
    assert quote_in_chunk("built kafka pipelines processing", R1.text)
    assert not quote_in_chunk("built kafka streams", R1.text)


def test_validate_matches_drops_ungrounded_and_downgrades() -> None:
    reqs = [req() for _ in range(4)]
    drafts = [
        draft(0, "matched", "r1", "Built Kafka pipelines"),
        draft(1, "matched", "r1", "invented quote"),
        draft(2, "partial", "j1", "Needs Kafka pipelines"),
        draft(3, "matched", "r9", "Built Kafka pipelines"),
    ]
    out, downgraded = validate_matches(drafts, reqs, LABELLED)
    assert [m.status for m in out] == ["matched", "missing", "missing", "missing"]
    assert len(out[0].evidence) == 1
    assert out[0].evidence[0].chunk_id == R1.chunk_id
    assert all(m.evidence == [] for m in out[1:])
    assert downgraded == 3


def test_validate_matches_fills_unmatched_requirements() -> None:
    out, downgraded = validate_matches(
        [draft(0, "matched", "r1", "built kafka")], [req(), req("Go")], LABELLED
    )
    assert len(out) == 2
    assert out[1].status == "missing"
    assert downgraded == 0


def test_label_pattern_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        EvidenceDraft(source="x1", quote="q")
