"""Chat request and SSE event schemas."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, StringConstraints

from app.guardrails.base import GuardrailOutcome, Mode


class ChatRequest(BaseModel):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class Citation(BaseModel):
    label: str
    chunk_id: UUID
    doc_type: str
    section: str
    text: str


class ChatProgress(BaseModel):
    step: Literal["checking_input", "retrieving", "reranking", "generating", "checking_answer"]


class TokenEvent(BaseModel):
    text: str


class Verdict(BaseModel):
    """Result of the output guard, sent just before ``done``. ``grounded`` is False only
    when an enforce-mode flag fired; monitor flags stay ``allow`` but appear in ``done``."""

    grounded: bool
    action: Literal["allow", "flag"]
    unsupported_claims: list[str]
    mode: Mode | None


class ChatDone(BaseModel):
    """Spec section 7.5 ``ChatAnswer``: the final event of a chat stream."""

    answer: str
    citations: list[Citation]
    invalid_citations: list[str]
    refused: bool
    prompt_version: str
    request_id: str
    guardrails: list[GuardrailOutcome]
    blocked: bool
