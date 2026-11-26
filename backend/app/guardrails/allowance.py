"""Per-user daily allowance and per-minute burst limit, enforced by atomic Postgres counters."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user_id
from app.core.errors import AppError
from app.core.request_id import request_id_var
from app.core.settings import Settings, get_settings
from app.db.engine import get_sessionmaker
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.recorder import DbRecorder

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
    limit: int = getattr(settings, f"allowance_{kind}")
    if not await _bump(session, user_id, "burst", minute, settings.burst_per_minute):
        await session.rollback()
        raise RateLimited
    if not await _bump(session, user_id, kind, day, limit):
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
    """``consume`` in its own session; a refusal is recorded as a guardrail event first."""
    try:
        async with sessionmaker() as session:
            await consume(session, user_id, kind, settings=settings)
    except (QuotaExceeded, RateLimited) as exc:
        outcome = GuardrailOutcome(
            guardrail="allowance",
            provider="self",
            stage="chat_input",
            action="block",
            mode="enforce",
            reason=f"quota:{kind}" if isinstance(exc, QuotaExceeded) else "burst",
            latency_ms=0,
            items=[],
        )
        ctx = GuardContext(
            stage="chat_input", texts=(), request_id=request_id_var.get(), user_id=user_id
        )
        await DbRecorder(sessionmaker).record(ctx, outcome)
        raise


def allowance(kind: Kind) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency that spends one ``kind`` request or answers 429."""

    async def spend(
        user_id: Annotated[UUID, Depends(get_current_user_id)],
        sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> None:
        await consume_or_refuse(sessionmaker, user_id, kind, settings=settings)

    return spend
