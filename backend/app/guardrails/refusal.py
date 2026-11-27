"""Shared recording of an enforced refusal (allowance, spend cap) as a guardrail event."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.request_id import request_id_var
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.pipeline import record_outcome
from app.guardrails.recorder import DbRecorder


async def record_refusal(
    sessionmaker: async_sessionmaker[AsyncSession], user_id: UUID, guardrail: str, reason: str
) -> None:
    outcome = GuardrailOutcome(
        guardrail=guardrail,
        provider="self",
        stage="chat_input",
        action="block",
        mode="enforce",
        reason=reason,
        latency_ms=0,
        items=[],
    )
    ctx = GuardContext(
        stage="chat_input", texts=(), request_id=request_id_var.get(), user_id=user_id
    )
    await record_outcome(DbRecorder(sessionmaker), ctx, outcome)
