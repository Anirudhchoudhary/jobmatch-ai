"""Retriever: candidate generation (vector or hybrid RRF) and a separate rerank step."""

from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.llm.embeddings import Embedder
from app.llm.providers import Providers, get_providers
from app.llm.rerank import Reranker
from app.retrieval.fusion import rrf_fuse
from app.retrieval.search import RetrievedChunk, active_doc_ids, text_search, vector_search


class Retriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        reranker: Reranker,
        *,
        mode: Literal["vector", "hybrid"],
        k: int,
        rrf_k: int,
        top_n: int,
    ) -> None:
        self._sm = sessionmaker
        self._embedder = embedder
        self._reranker = reranker
        self._mode = mode
        self._k = k
        self._rrf_k = rrf_k
        self._top_n = top_n

    async def candidates(self, user_id: UUID, query: str) -> list[RetrievedChunk]:
        """Vector hits (``vector`` mode) or RRF of vector and full-text hits (otherwise)."""
        async with self._sm() as session:
            doc_ids = await active_doc_ids(session, user_id, embed_model=self._embedder.embed_model)
            if not doc_ids:
                return []
            embedding = await self._embedder.embed_query(query)
            vec = await vector_search(
                session, user_id=user_id, doc_ids=doc_ids, query_embedding=embedding, k=self._k
            )
            if self._mode == "vector":
                return vec
            txt = await text_search(
                session, user_id=user_id, doc_ids=doc_ids, query=query, k=self._k
            )
        return rrf_fuse([vec, txt], k=self._rrf_k)

    async def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Keep ``top_n`` chunks ordered by reranker relevance (incoming order if unscored)."""
        if not chunks:
            return []
        hits = await self._reranker.rerank(query, [c.text for c in chunks], self._top_n)
        scored = [(h, chunks[h.index]) for h in hits if 0 <= h.index < len(chunks)]
        if any(h.relevance is not None for h, _ in scored):
            scored.sort(
                key=lambda p: p[0].relevance if p[0].relevance is not None else -1.0, reverse=True
            )
        return [replace(c, relevance=h.relevance) for h, c in scored[: self._top_n]]


def get_retriever(
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    providers: Annotated[Providers, Depends(get_providers)],
) -> Retriever:
    s = get_settings()
    return Retriever(
        sessionmaker,
        providers.embedder,
        providers.reranker,
        mode=s.retrieval_mode,
        k=s.retrieval_k,
        rrf_k=s.rrf_k,
        top_n=s.rerank_top_n,
    )
