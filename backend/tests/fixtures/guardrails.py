"""Fake guards and recorder for guardrail tests."""

from app.guardrails.base import GuardContext, GuardrailOutcome, GuardrailResult


class ListRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[GuardContext, GuardrailOutcome]] = []

    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        self.records.append((ctx, outcome))


class StaticGuard:
    provider = "fake"

    def __init__(self, result: GuardrailResult) -> None:
        self.result = result
        self.name = result.guardrail
        self.calls: list[GuardContext] = []

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        self.calls.append(ctx)
        return self.result


class RaisingGuard:
    name = "raising"
    provider = "fake"

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        raise RuntimeError("boom")
