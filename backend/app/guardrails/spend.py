"""Global daily spend cap: a backstop that refuses requests once today's LLM cost hits the cap."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.core.settings import Settings
from app.db.models import RequestLog
from app.guardrails.refusal import record_refusal


class DemoAtCapacity(AppError):
    def __init__(self) -> None:
        super().__init__(
            "demo_at_capacity", "The demo is at capacity today. Please try again tomorrow.", 503
        )


async def spent_today(session: AsyncSession, *, now: datetime | None = None) -> float:
    """Total ``cost_usd`` logged since 00:00 UTC today (0 when nothing is logged)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    total = await session.scalar(
        select(func.coalesce(func.sum(RequestLog.cost_usd), 0)).where(RequestLog.created_at >= day)
    )
    return float(total or 0)


async def ensure_budget(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID, *, settings: Settings
) -> None:
    """Refuse (503, recorded as a guardrail event) when today's spend has reached the cap."""
    cap = settings.daily_spend_cap_usd
    if cap is None:
        return
    async with sessionmaker() as session:
        spent = await spent_today(session)
    if spent >= cap:
        await record_refusal(sessionmaker, user_id, "spend_cap", "spend_cap")
        raise DemoAtCapacity
