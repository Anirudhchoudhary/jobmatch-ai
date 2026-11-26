"""Persists guardrail outcomes to ``guardrail_events`` (PII-free by construction)."""

import anyio
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import GuardrailEvent
from app.guardrails.base import GuardContext, GuardrailOutcome

log = structlog.get_logger(__name__)
_WRITE_TIMEOUT_S = 5


class DbRecorder:
    """Writes each outcome in its own session, shielded so a client disconnect cannot drop it."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        # Shielded so a client disconnect cannot drop the row; the timeout bounds the shield.
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(_WRITE_TIMEOUT_S) as scope:
                async with self._sessionmaker() as session:
                    session.add(
                        GuardrailEvent(
                            request_id=ctx.request_id,
                            user_id=ctx.user_id,
                            stage=outcome.stage,
                            guardrail=outcome.guardrail,
                            provider=outcome.provider,
                            action=outcome.action,
                            mode=outcome.mode,
                            reason=outcome.reason,
                            latency_ms=outcome.latency_ms,
                            item_count=len(outcome.items) or len(ctx.texts),
                        )
                    )
                    await session.commit()
            if scope.cancelled_caught:
                log.warning("guardrail_record_timeout", guardrail=outcome.guardrail)
