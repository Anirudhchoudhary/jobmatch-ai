"""Runs guards in order, applying enforce/monitor semantics and recording outcomes."""

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

import structlog

from app.core.logging import redact_pii
from app.core.request_id import request_id_var
from app.guardrails.base import (
    GuardContext,
    Guardrail,
    GuardrailOutcome,
    GuardrailResult,
    Mode,
    Stage,
)

log = structlog.get_logger(__name__)


class GuardrailRecorder(Protocol):
    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None: ...


class NullRecorder:
    """Records nothing (tests and offline scripts)."""

    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        return None


@dataclass(frozen=True)
class GuardStep:
    guard: Guardrail
    mode: Mode


@dataclass(frozen=True)
class PipelineResult:
    texts: tuple[str, ...]
    blocked_items: frozenset[int]
    blocked: bool
    flagged: bool
    outcomes: tuple[GuardrailOutcome, ...]


def _outcome(result: GuardrailResult, stage: Stage, mode: Mode) -> GuardrailOutcome:
    return GuardrailOutcome(
        guardrail=result.guardrail,
        provider=result.provider,
        stage=stage,
        action=result.action,
        mode=mode,
        reason=redact_pii(result.reason),
        latency_ms=result.latency_ms,
        items=list(result.items),
    )


async def record_outcome(
    recorder: GuardrailRecorder, ctx: GuardContext, outcome: GuardrailOutcome
) -> None:
    """Log the event (``guardrail_error`` for errors, else ``guardrail_fired``) and persist it.

    The single place guardrail events are emitted. Never raises: a failed audit write is
    logged with the error type only, so the request still completes.
    """
    log.info(
        "guardrail_error" if outcome.action == "error" else "guardrail_fired",
        guardrail=outcome.guardrail,
        provider=outcome.provider,
        stage=outcome.stage,
        action=outcome.action,
        mode=outcome.mode,
        reason=outcome.reason,
        item_count=len(outcome.items) or len(ctx.texts),
    )
    try:
        await recorder.record(ctx, outcome)
    except Exception as exc:
        log.warning(
            "guardrail_record_failed",
            guardrail=outcome.guardrail,
            stage=outcome.stage,
            error_type=type(exc).__name__,
        )


class GuardrailPipeline:
    def __init__(
        self, stage: Stage, steps: Sequence[GuardStep], recorder: GuardrailRecorder
    ) -> None:
        self._stage = stage
        self._steps = tuple(steps)
        self._recorder = recorder

    @property
    def recorder(self) -> GuardrailRecorder:
        return self._recorder

    @property
    def steps(self) -> tuple[GuardStep, ...]:
        return self._steps

    async def run(self, ctx: GuardContext) -> PipelineResult:
        blocked = False
        flagged = False
        blocked_items: set[int] = set()
        outcomes: list[GuardrailOutcome] = []

        for step in self._steps:
            guard = step.guard
            started = time.perf_counter()
            try:
                result = await guard.check(ctx)
            except Exception as exc:
                outcome = GuardrailOutcome(
                    guardrail=guard.name,
                    provider=guard.provider,
                    stage=self._stage,
                    action="error",
                    mode=step.mode,
                    reason=f"guard_error:{type(exc).__name__}",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    items=[],
                )
                outcomes.append(outcome)
                await record_outcome(self._recorder, ctx, outcome)
                continue

            outcome = _outcome(result, self._stage, step.mode)
            outcomes.append(outcome)
            if result.action == "allow":
                continue

            await record_outcome(self._recorder, ctx, outcome)
            if step.mode == "monitor":
                continue

            if result.action == "flag":
                flagged = True
            elif result.action == "redact" and result.redacted is not None:
                ctx = replace(ctx, texts=result.redacted)
            elif result.action == "block":
                if result.items:
                    blocked_items.update(result.items)
                    if blocked_items.issuperset(range(len(ctx.texts))):
                        blocked = True
                        break
                else:
                    blocked = True
                    break

        return PipelineResult(
            texts=ctx.texts,
            blocked_items=frozenset(blocked_items),
            blocked=blocked,
            flagged=flagged,
            outcomes=tuple(outcomes),
        )


async def record_hallucination_block(
    recorder: GuardrailRecorder, user_id: UUID, reason: str
) -> None:
    """Record that a grounding check dropped model output. Never raises: the analysis or
    suggestions the user asked for must still be returned if the audit row cannot be written."""
    outcome = GuardrailOutcome(
        guardrail="hallucination_block",
        provider="self",
        stage="output",
        action="block",
        mode="enforce",
        reason=reason,
        latency_ms=0,
        items=[],
    )
    await record_outcome(
        recorder, GuardContext("output", (), request_id_var.get(), user_id), outcome
    )
