"""Data lifecycle: purge text-bearing rows older than the retention window (spec 7.9)."""

from datetime import datetime, timedelta

import structlog
from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Document, LoginCode, SemanticCacheEntry, UsageCounter

log = structlog.get_logger(__name__)


async def purge_expired(
    sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime, retention: timedelta
) -> dict[str, int]:
    """Delete expired rows in one transaction and return the count deleted per table.

    Documents cascade to chunks and analyses. Users, guardrail_events and request_log are kept.
    """
    cutoff = now - retention
    statements = {
        "documents": delete(Document).where(Document.created_at < cutoff),
        "semantic_cache": delete(SemanticCacheEntry).where(
            or_(SemanticCacheEntry.expires_at < now, SemanticCacheEntry.created_at < cutoff)
        ),
        "login_codes": delete(LoginCode).where(LoginCode.created_at < cutoff),
        "usage_counters": delete(UsageCounter).where(UsageCounter.window_start < cutoff),
    }
    counts: dict[str, int] = {}
    async with sessionmaker() as session, session.begin():
        for table, stmt in statements.items():
            result = await session.execute(stmt)
            counts[table] = int(result.rowcount)  # type: ignore[attr-defined]
    log.info("cleanup_completed", **counts)
    return counts
