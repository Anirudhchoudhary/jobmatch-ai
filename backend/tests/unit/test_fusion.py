from uuid import uuid4

from app.retrieval.fusion import rrf_fuse
from app.retrieval.search import RetrievedChunk


def mk(label: str) -> RetrievedChunk:
    return RetrievedChunk(uuid4(), uuid4(), "resume", "Skills", 0, label, 0.0)


def test_rrf_scores_and_order() -> None:
    x, y, z, w = mk("x"), mk("y"), mk("z"), mk("w")
    fused = rrf_fuse([[x, y, z], [y, w]], k=60)
    assert [c.text for c in fused] == ["y", "x", "w", "z"]
    assert fused[0].score == 1 / 61 + 1 / 62
    assert fused[1].score == 1 / 61


def test_rrf_dedup() -> None:
    x = mk("x")
    fused = rrf_fuse([[x], [x], [x]])
    assert len(fused) == 1
    assert fused[0].chunk_id == x.chunk_id


def test_rrf_empty() -> None:
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []
