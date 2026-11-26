"""Guardrail types shared by every guard, the pipeline and the event store."""

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

Stage = Literal["upload", "chat_input", "output"]
Action = Literal["allow", "redact", "flag", "block"]
Mode = Literal["enforce", "monitor"]

ACTION_RANK: dict[Action, int] = {"allow": 0, "flag": 1, "redact": 2, "block": 3}


@dataclass(frozen=True)
class GuardContext:
    """What a guard inspects. ``texts`` are the items checked; ``sources`` is cited context."""

    stage: Stage
    texts: tuple[str, ...]
    request_id: str
    user_id: UUID | None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardrailResult:
    """A guard's verdict. ``items`` are indexes of ``texts`` it applies to (empty = all)."""

    guardrail: str
    provider: str
    action: Action
    reason: str
    latency_ms: int
    items: tuple[int, ...] = ()
    redacted: tuple[str, ...] | None = None


class Guardrail(Protocol):
    name: str
    provider: str

    async def check(self, ctx: GuardContext) -> GuardrailResult: ...


class GuardrailOutcome(BaseModel):
    """Public, PII-free record of one guard's verdict at one stage."""

    guardrail: str
    provider: str
    stage: Stage
    action: Action | Literal["error"]
    mode: Mode
    reason: str
    latency_ms: int
    items: list[int]
