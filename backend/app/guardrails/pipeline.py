"""Runs guards in order, applying enforce/monitor semantics and recording outcomes."""

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import structlog

from app.core.logging import redact_pii
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

    async def _record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        try:
            await self._recorder.record(ctx, outcome)
        except Exception as exc:
            log.warning(
                "guardrail_record_failed",
                guardrail=outcome.guardrail,
                stage=self._stage,
                error_type=type(exc).__name__,
            )

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
                log.warning(
                    "guardrail_error",
                    guardrail=guard.name,
                    stage=self._stage,
                    error_type=type(exc).__name__,
                )
                outcomes.append(outcome)
                await self._record(ctx, outcome)
                continue

            outcome = _outcome(result, self._stage, step.mode)
            outcomes.append(outcome)
            if result.action == "allow":
                continue

            log.info(
                "guardrail_fired",
                guardrail=outcome.guardrail,
                provider=outcome.provider,
                stage=self._stage,
                action=outcome.action,
                mode=step.mode,
                reason=outcome.reason,
                item_count=len(result.items) or len(ctx.texts),
            )
            await self._record(ctx, outcome)
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
