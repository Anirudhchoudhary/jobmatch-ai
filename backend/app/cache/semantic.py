"""pgvector semantic cache for grounded chat answers.

Rows are scoped by user and by a hash of the active document set (plus embedder, prompt
version and chat model), so a new upload or a prompt/model change never serves a stale
answer. Failures are logged by error type only and treated as a miss (lookup) or a no-op
(store). The question and answer text are never logged.
"""

import hashlib
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol
from uuid import UUID

import anyio
import structlog
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import SemanticCacheEntry
from app.schemas.chat import Citation

log = structlog.get_logger(__name__)
_TIMEOUT_S = 5


class CachedAnswer(BaseModel):
    answer: str
    citations: list[Citation]
    invalid_citations: list[str]


class ChatCache(Protocol):
    async def lookup(
        self, user_id: UUID, key: str, embedding: list[float]
    ) -> CachedAnswer | None: ...

    async def store(
        self, user_id: UUID, key: str, embedding: list[float], answer: CachedAnswer
    ) -> None: ...


def doc_set_key(
    doc_ids: Sequence[UUID], *, embed_model: str, prompt_version: str, model: str
) -> str:
    raw = "|".join(sorted(str(d) for d in doc_ids)) + f"|{embed_model}|{prompt_version}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def invalidate_user_cache(session: AsyncSession, user_id: UUID) -> None:
    """Delete the user's cache rows; runs in the caller's transaction."""
    await session.execute(delete(SemanticCacheEntry).where(SemanticCacheEntry.user_id == user_id))


class SemanticCache:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        threshold: float,
        ttl: timedelta,
    ) -> None:
        self._sm = sessionmaker
        self._threshold = threshold
        self._ttl = ttl

    async def lookup(self, user_id: UUID, key: str, embedding: list[float]) -> CachedAnswer | None:
        """The nearest live entry for this user and key, if its cosine >= threshold."""
        try:
            with anyio.move_on_after(_TIMEOUT_S) as scope:
                distance = SemanticCacheEntry.embedding.cosine_distance(embedding)
                stmt = (
                    select(SemanticCacheEntry.payload, distance.label("distance"))
                    .where(
                        SemanticCacheEntry.user_id == user_id,
                        SemanticCacheEntry.doc_set_hash == key,
                        SemanticCacheEntry.expires_at > func.now(),
                    )
                    .order_by(distance)
                    .limit(1)
                )
                async with self._sm() as session:
                    row = (await session.execute(stmt)).first()
                if row is None or 1 - float(row.distance) < self._threshold:
                    return None
                return CachedAnswer.model_validate(row.payload)
            if scope.cancelled_caught:
                log.warning("semantic_cache_error", op="lookup", error_type="Timeout")
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic_cache_error", op="lookup", error_type=type(exc).__name__)
        return None

    async def store(
        self, user_id: UUID, key: str, embedding: list[float], answer: CachedAnswer
    ) -> None:
        try:
            # Shielded so a client disconnect after the answer cannot drop the row; the
            # timeout bounds the shield.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(_TIMEOUT_S) as scope:
                    async with self._sm() as session:
                        session.add(
                            SemanticCacheEntry(
                                user_id=user_id,
                                doc_set_hash=key,
                                embedding=embedding,
                                payload=answer.model_dump(mode="json"),
                                expires_at=func.now() + self._ttl,
                            )
                        )
                        await session.commit()
                if scope.cancelled_caught:
                    log.warning("semantic_cache_error", op="store", error_type="Timeout")
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic_cache_error", op="store", error_type=type(exc).__name__)
