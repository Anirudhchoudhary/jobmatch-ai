from collections.abc import Iterator
from uuid import UUID

import boto3
import pytest
from botocore.client import Config
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_id
from app.core.settings import get_settings
from app.ingestion.blobs import get_blob_store
from app.ingestion.s3_blobs import S3BlobStore
from app.main import create_app

ME = UUID("11111111-1111-4111-8111-111111111111")
BUCKET = "test-uploads"


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("BLOB_BACKEND", "s3")
    monkeypatch.setenv("UPLOADS_BUCKET", BUCKET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _store() -> S3BlobStore:
    client = boto3.client(
        "s3",
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(signature_version="s3v4"),
    )
    return S3BlobStore(client, BUCKET)


def test_create_upload_s3_returns_presigned_ticket(s3_env: None) -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: ME
    app.dependency_overrides[get_blob_store] = _store
    resp = TestClient(app).post(
        "/api/uploads", json={"content_type": "application/pdf", "size_bytes": 10}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"].startswith(f"uploads/{ME}/")
    assert body["method"] == "PUT"
    assert body["url"].startswith("https://") and body["key"] in body["url"]
    assert "X-Amz-Signature" in body["url"]
    assert body["headers"] == {
        "Content-Type": "application/pdf",
        "x-amz-server-side-encryption": "AES256",
    }


def test_local_put_route_absent_with_s3_backend(s3_env: None) -> None:
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: ME
    app.dependency_overrides[get_blob_store] = _store
    resp = TestClient(app).put(
        f"/api/uploads/uploads/{ME}/{'a' * 8}-aaaa-aaaa-aaaa-{'b' * 12}.pdf",
        content=b"%PDF-",
        headers={"Content-Type": "application/pdf"},
    )
    assert resp.status_code in (404, 405)
