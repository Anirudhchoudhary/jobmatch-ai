"""Result models produced by the eval runner and consumed by metrics (JSON-serialisable)."""

from typing import Literal

from pydantic import BaseModel

from evals.dataset import Expected, QAKind


class Fired(BaseModel):
    """A guardrail firing. Only non-`allow` outcomes are stored."""

    guardrail: str
    provider: str
    action: str
    mode: str


class DocResult(BaseModel):
    doc_type: Literal["resume", "jd"]
    attack: bool
    quarantined: int
    fired: list[Fired]


class ChatResult(BaseModel):
    qa_id: str
    question: str
    kind: QAKind
    expect_refusal: bool
    answer: str
    refused: bool
    blocked_by: str | None
    grounded: bool | None  # groundedness verdict; None when not evaluated
    fired: list[Fired]
    retrieved: list[str]  # chunk texts passed to generation
    supporting: list[str]
    expected_answer: str | None
    judge: dict[str, float] = {}


class AnalysisResult(BaseModel):
    score: int
    skills: list[tuple[str, str]]  # (requirement skill, status)
    suggestions: int
    rejected: int


class CaseResult(BaseModel):
    case_id: str
    category: str
    variant: str
    docs: list[DocResult]
    analysis: AnalysisResult | None
    expected: Expected
    chats: list[ChatResult]
    pii_leaks: int
    cost_usd: float
    latency_ms: int
    errors: list[str]  # error codes only, never messages containing text
