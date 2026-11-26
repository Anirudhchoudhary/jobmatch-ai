"""Upload request/response schemas."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class UploadRequest(BaseModel):
    content_type: Literal["application/pdf"]
    size_bytes: int = Field(ge=1)
    filename: str | None = Field(default=None, max_length=200)


class UploadTicket(BaseModel):
    key: str
    url: str
    method: Literal["PUT"]
    headers: dict[str, str]
    max_bytes: int


class DocumentRequest(BaseModel):
    doc_type: Literal["resume", "jd"]
    upload_key: str | None = None
    text: str | None = None
    filename: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        if (self.upload_key is None) == (self.text is None):
            raise ValueError("provide exactly one of upload_key and text")
        if self.doc_type == "resume" and self.upload_key is None:
            raise ValueError("a resume requires upload_key")
        return self


class ProgressEvent(BaseModel):
    step: Literal["validating", "parsing", "chunking", "embedding", "storing"]
    status: Literal["started", "done"]


class DocumentReady(BaseModel):
    document_id: UUID
    doc_type: Literal["resume", "jd"]
    chunk_count: int
    reused: bool
