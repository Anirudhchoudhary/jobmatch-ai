"""Chat request and SSE event schemas."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, StringConstraints


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
    step: Literal["retrieving", "reranking", "generating"]


class TokenEvent(BaseModel):
    text: str


class ChatDone(BaseModel):
    """Spec section 7.5 ``ChatAnswer``: the final event of a chat stream."""

    answer: str
    citations: list[Citation]
    invalid_citations: list[str]
    refused: bool
    prompt_version: str
    request_id: str
