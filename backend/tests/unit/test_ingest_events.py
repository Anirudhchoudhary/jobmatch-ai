from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.settings import Settings
from app.ingestion.blobs import LocalBlobStore
from app.ingestion.service import ingest_document
from app.llm.embeddings import HashEmbedder
from app.schemas.documents import DocumentReady, DocumentRequest, ProgressEvent

USER = UUID("11111111-1111-4111-8111-111111111111")
JD = "We are hiring a Python backend engineer to build FastAPI services and data pipelines. " * 3


class FakeResult:
    def first(self) -> None:
        return None


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, stmt: object) -> FakeResult:
        self.log.append("execute")
        return FakeResult()

    def add(self, obj: object) -> None:
        self.log.append("add")

    def add_all(self, objs: Any) -> None:
        self.log.append(f"add_all:{len(list(objs))}")

    async def commit(self) -> None:
        self.log.append("commit")


class FakeSessionmaker:
    def __init__(self) -> None:
        self.log: list[str] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self.log)


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    return Settings()  # type: ignore[call-arg]


async def test_ingest_event_sequence(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    sm = FakeSessionmaker()
    events = [
        e
        async for e in ingest_document(
            user_id=USER,
            request=DocumentRequest(doc_type="jd", text=JD),
            sessionmaker=sm,  # type: ignore[arg-type]
            blobs=LocalBlobStore(tmp_path),
            embedder=HashEmbedder(),
            settings=_settings(monkeypatch),
        )
    ]
    steps = ["validating", "parsing", "chunking", "embedding", "storing"]
    expected = [(s, st) for s in steps for st in ("started", "done")]
    progress = [
        (e.step, e.status)
        for name, e in events
        if name == "progress" and isinstance(e, ProgressEvent)
    ]
    assert progress == expected
    name, last = events[-1]
    assert name == "done"
    assert isinstance(last, DocumentReady)
    assert last.reused is False
    assert last.chunk_count >= 1
    assert sm.log.count("commit") == 2


@pytest.mark.parametrize(
    "body",
    [
        {"doc_type": "jd"},
        {"doc_type": "jd", "text": "x", "upload_key": "k"},
        {"doc_type": "resume", "text": "x"},
        {"doc_type": "other", "text": "x"},
    ],
)
def test_document_request_validation(body: dict[str, Sequence[str] | str]) -> None:
    with pytest.raises(ValidationError):
        DocumentRequest.model_validate(body)
