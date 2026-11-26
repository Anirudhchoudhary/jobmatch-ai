"""Upload request/response schemas."""

from typing import Literal

from pydantic import BaseModel, Field


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
