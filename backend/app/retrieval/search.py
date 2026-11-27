"""Single-stage searches over chunks: pgvector cosine and Postgres full-text.

Every query is scoped by ``user_id`` and by the caller's ``ready`` document ids (embedded by
the current embedder), and all values are bound parameters.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk with a stage-dependent ``score`` (higher is better within one stage).

    ``score`` means cosine similarity (1 - distance) from ``vector_search``, ``ts_rank_cd``
    from ``text_search``, and the summed reciprocal-rank value after ``rrf_fuse``. Scores are
    not comparable across stages. ``relevance`` is set only by the reranker, when it scores.
    """

    chunk_id: UUID
    doc_id: UUID
    doc_type: str
    section: str
    ordinal: int
    text: str
    score: float
    relevance: float | None = None


async def active_doc_ids(session: AsyncSession, user_id: UUID, *, embed_model: str) -> list[UUID]:
    """The user's ready documents whose vectors came from ``embed_model`` (others are stale)."""
    rows = await session.execute(
        select(Document.id).where(
            Document.user_id == user_id,
            Document.status == "ready",
            Document.embed_model == embed_model,
        )
    )
    return list(rows.scalars())


def _scoped(
    stmt: Select[tuple[Chunk, float]], user_id: UUID, doc_ids: list[UUID]
) -> Select[tuple[Chunk, float]]:
    return stmt.where(
        Chunk.user_id == user_id,
        Chunk.doc_id.in_(doc_ids),
        Chunk.quarantined.is_(False),
    )


def _to_chunk(c: Chunk, score: float) -> RetrievedChunk:
    return RetrievedChunk(c.id, c.doc_id, c.doc_type, c.section, c.ordinal, c.text, float(score))


async def vector_search(
    session: AsyncSession,
    *,
    user_id: UUID,
    doc_ids: list[UUID],
    query_embedding: list[float],
    k: int,
) -> list[RetrievedChunk]:
    if not doc_ids:
        return []
    # Same transaction as the query below: lets HNSW keep scanning past rows that the
    # user/doc filter discards (pgvector 0.8+), so filtering never starves the result set.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    distance = Chunk.embedding.cosine_distance(query_embedding)
    stmt = _scoped(select(Chunk, (1 - distance).label("score")), user_id, doc_ids)
    rows = await session.execute(
        stmt.order_by(distance, Chunk.doc_type, Chunk.ordinal, Chunk.doc_id).limit(k)
    )
    return [_to_chunk(r.Chunk, r.score) for r in rows.all()]


async def text_search(
    session: AsyncSession,
    *,
    user_id: UUID,
    doc_ids: list[UUID],
    query: str,
    k: int,
) -> list[RetrievedChunk]:
    # Blank input yields an empty tsquery (Postgres raises a NOTICE); skip the round trip.
    if not doc_ids or not query.strip():
        return []
    tsq = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(Chunk.tsv, tsq)
    stmt = _scoped(select(Chunk, rank.label("score")), user_id, doc_ids)
    rows = await session.execute(
        stmt.where(Chunk.tsv.op("@@")(tsq))
        .order_by(rank.desc(), Chunk.doc_type, Chunk.ordinal, Chunk.doc_id)  # run-stable ties
        .limit(k)
    )
    return [_to_chunk(r.Chunk, r.score) for r in rows.all()]
