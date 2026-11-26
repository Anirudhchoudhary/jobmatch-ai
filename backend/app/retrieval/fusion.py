"""Reciprocal rank fusion."""

from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from app.retrieval.search import RetrievedChunk


def rrf_fuse(rankings: Sequence[Sequence[RetrievedChunk]], *, k: int = 60) -> list[RetrievedChunk]:
    """Merge rankings: score = sum of 1/(k + rank), rank 1-based, deduplicated by chunk id.

    Ties keep first-appearance order (dicts preserve insertion order and ``sorted`` is stable).
    """
    scores: dict[UUID, float] = {}
    first: dict[UUID, RetrievedChunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            first.setdefault(chunk.chunk_id, chunk)
    order = sorted(first, key=lambda cid: scores[cid], reverse=True)
    return [replace(first[cid], score=scores[cid], relevance=None) for cid in order]
