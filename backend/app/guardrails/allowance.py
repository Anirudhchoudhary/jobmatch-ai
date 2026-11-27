"""Per-user daily allowance and per-minute burst limit, enforced by atomic Postgres counters.

Handlers call ``consume_or_refuse`` after request validation, so a malformed request (422) is
never charged. A request that is accepted and later fails (ingestion or provider errors) still
consumes its allowance.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.core.settings import Settings
from app.guardrails.refusal import record_refusal
from app.guardrails.spend import ensure_budget

Kind = Literal["analyses", "uploads", "chats", "suggestions"]

# One statement per window: bump the count unless the limit is already reached.
# No row returned means the window is full.
_BUMP = text(
    """
    INSERT INTO usage_counters (user_id, kind, window_start, count)
    VALUES (:user_id, :kind, :window_start, 1)
    ON CONFLICT (user_id, kind, window_start)
    DO UPDATE SET count = usage_counters.count + 1
    WHERE usage_counters.count < :limit
    RETURNING count
    """
)


class QuotaExceeded(AppError):
    def __init__(self, kind: Kind, limit: int) -> None:
        super().__init__(
            "quota_exceeded",
            f"You've used today's {limit} {kind}. It resets at midnight UTC.",
            429,
        )
        self.kind = kind


class RateLimited(AppError):
    def __init__(self) -> None:
        super().__init__("rate_limited", "Too many requests; wait a minute and try again.", 429)


async def _bump(
    session: AsyncSession, user_id: UUID, kind: str, window_start: datetime, limit: int
) -> bool:
    result = await session.execute(
        _BUMP,
        {"user_id": user_id, "kind": kind, "window_start": window_start, "limit": limit},
    )
    return result.first() is not None


async def consume(
    session: AsyncSession,
    user_id: UUID,
    kind: Kind,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Count one request against the burst and daily windows in one transaction.

    Commits on success (the count must outlive a streaming body); rolls back and raises
    ``RateLimited`` / ``QuotaExceeded`` when a window is full.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    minute = moment.replace(second=0, microsecond=0)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    limits: dict[Kind, int] = {
        "analyses": settings.allowance_analyses,
        "uploads": settings.allowance_uploads,
        "chats": settings.allowance_chats,
        "suggestions": settings.allowance_suggestions,
    }
    limit = limits[kind]
    # A zero limit means "none allowed"; the INSERT path of _bump would admit the first request.
    if settings.burst_per_minute <= 0 or not await _bump(
        session, user_id, "burst", minute, settings.burst_per_minute
    ):
        await session.rollback()
        raise RateLimited
    if limit <= 0 or not await _bump(session, user_id, kind, day, limit):
        await session.rollback()
        raise QuotaExceeded(kind, limit)
    await session.commit()


async def consume_or_refuse(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: UUID,
    kind: Kind,
    *,
    settings: Settings,
) -> None:
    """Spend-cap check, then ``consume`` in its own session; a refusal is recorded first."""
    await ensure_budget(sessionmaker, user_id, settings=settings)
    try:
        async with sessionmaker() as session:
            await consume(session, user_id, kind, settings=settings)
    except (QuotaExceeded, RateLimited) as exc:
        reason = f"quota:{kind}" if isinstance(exc, QuotaExceeded) else "burst"
        await record_refusal(sessionmaker, user_id, "allowance", reason)
        raise
