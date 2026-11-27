from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.ingestion.blobs import LocalBlobStore, get_blob_store, new_upload_key
from app.main import create_app

ME = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
PDF = {"Content-Type": "application/pdf"}


def _wire(app: FastAPI, store: LocalBlobStore) -> TestClient:
    app.dependency_overrides[get_current_user_id] = lambda: ME
    app.dependency_overrides[get_blob_store] = lambda: store
    return TestClient(app)


@pytest.fixture
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path)


@pytest.fixture
def client(store: LocalBlobStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENV", "local")
    get_settings.cache_clear()
    yield _wire(create_app(), store)
    get_settings.cache_clear()


def _ticket(client: TestClient, size: int = 10) -> dict[str, object]:
    resp = client.post("/api/uploads", json={"content_type": "application/pdf", "size_bytes": size})
    assert resp.status_code == 200
    body: dict[str, object] = resp.json()
    return body


async def test_ticket_then_put(client: TestClient, store: LocalBlobStore) -> None:
    ticket = _ticket(client)
    key = str(ticket["key"])
    assert key.startswith(f"uploads/{ME}/")
    assert ticket["url"] == f"/api/uploads/{key}"
    assert ticket["method"] == "PUT"
    assert ticket["headers"] == PDF
    assert ticket["max_bytes"] == 5_242_880
    resp = client.put(str(ticket["url"]), content=b"%PDF-1.7 hi", headers=PDF)
    assert resp.status_code == 204
    assert await store.get(key) == b"%PDF-1.7 hi"


def test_ticket_rejects_oversize(client: TestClient) -> None:
    resp = client.post(
        "/api/uploads", json={"content_type": "application/pdf", "size_bytes": 5_242_881}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "too_large"


def test_too_large_message_follows_configured_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2097152")
    get_settings.cache_clear()
    resp = client.post(
        "/api/uploads", json={"content_type": "application/pdf", "size_bytes": 2_097_153}
    )
    assert resp.status_code == 413
    assert "2 MB" in resp.json()["error"]["message"]
    key = str(_ticket(client)["key"])
    put = client.put(f"/api/uploads/{key}", content=b"a" * 2_097_153, headers=PDF)
    assert "2 MB" in put.json()["error"]["message"]


@pytest.mark.parametrize("size", [0, -1])
def test_ticket_rejects_bad_size(client: TestClient, size: int) -> None:
    resp = client.post("/api/uploads", json={"content_type": "application/pdf", "size_bytes": size})
    assert resp.status_code == 422


def test_ticket_rejects_non_pdf(client: TestClient) -> None:
    resp = client.post("/api/uploads", json={"content_type": "text/plain", "size_bytes": 5})
    assert resp.status_code == 422


def test_put_rejects_foreign_or_traversal_key(client: TestClient, tmp_path: Path) -> None:
    foreign = new_upload_key(OTHER)
    assert client.put(f"/api/uploads/{foreign}", content=b"%PDF-", headers=PDF).status_code == 404
    mine = new_upload_key(ME)
    for path in (
        f"/api/uploads/uploads/{ME}/%2e%2e/x.pdf",
        f"/api/uploads/uploads/{ME}/../x.pdf",
        f"/api/uploads/{mine}.txt",
    ):
        resp = client.put(path, content=b"%PDF-", headers=PDF)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] in ("upload_not_found", "not_found")
    assert not (tmp_path / "x.pdf").exists()


def test_put_rejects_oversize_body(client: TestClient, tmp_path: Path) -> None:
    key = str(_ticket(client)["key"])
    resp = client.put(f"/api/uploads/{key}", content=b"a" * 5_242_881, headers=PDF)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "too_large"
    assert not (tmp_path / key).exists()


def test_put_rejects_oversize_chunked_body_without_content_length(
    client: TestClient, tmp_path: Path
) -> None:
    key = str(_ticket(client)["key"])

    def gen() -> Iterator[bytes]:
        for _ in range(6):
            yield b"a" * 1_048_576

    resp = client.put(f"/api/uploads/{key}", content=gen(), headers=PDF)
    assert resp.status_code == 413
    assert not (tmp_path / key).exists()


def test_put_accepts_exactly_max(client: TestClient, tmp_path: Path) -> None:
    key = str(_ticket(client)["key"])
    resp = client.put(f"/api/uploads/{key}", content=b"a" * 5_242_880, headers=PDF)
    assert resp.status_code == 204
    assert (tmp_path / key).stat().st_size == 5_242_880


def test_put_requires_pdf_content_type(client: TestClient, tmp_path: Path) -> None:
    key = str(_ticket(client)["key"])
    resp = client.put(f"/api/uploads/{key}", content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_media_type"
    assert not (tmp_path / key).exists()


def test_put_not_mounted_in_prod(store: LocalBlobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    get_settings.cache_clear()
    client = _wire(create_app(), store)
    key = new_upload_key(ME)
    assert client.put(f"/api/uploads/{key}", content=b"%PDF-", headers=PDF).status_code in (
        404,
        405,
    )
    # ticket endpoint still exists
    assert (
        client.post(
            "/api/uploads", json={"content_type": "application/pdf", "size_bytes": 5}
        ).status_code
        == 200
    )
    get_settings.cache_clear()
