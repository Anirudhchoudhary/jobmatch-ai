"""App-level hardening: JSON-only state-changing /api requests, no API docs in prod."""

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_id
from app.main import create_app

ME = UUID("11111111-1111-4111-8111-111111111111")
PDF_KEY = f"uploads/{ME}/{'a' * 8}-aaaa-aaaa-aaaa-{'b' * 12}.pdf"


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENV", "local")


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"],
)
@pytest.mark.parametrize(
    "path", ["/api/auth/request-code", "/api/auth/logout", "/api/stream/chat", "/api/uploads"]
)
def test_non_json_post_to_api_is_415(local_env: None, path: str, content_type: str | None) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    resp = TestClient(create_app()).post(path, content=b'{"email": "a@b.co"}', headers=headers)
    assert resp.status_code == 415
    body = resp.json()["error"]
    assert body["code"] == "unsupported_media_type"
    assert body["request_id"] == resp.headers["X-Request-ID"]


def test_json_post_passes(local_env: None) -> None:
    client = TestClient(create_app())
    assert client.post("/api/auth/logout", json={}).status_code == 204
    with_charset = {"Content-Type": "application/json; charset=utf-8"}
    resp = client.post("/api/auth/logout", content=b"{}", headers=with_charset)
    assert resp.status_code == 204


def test_local_pdf_put_is_exempt(
    local_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    app = create_app()
    app.dependency_overrides[get_current_user_id] = lambda: ME
    client = TestClient(app)
    resp = client.put(
        f"/api/uploads/{PDF_KEY}", content=b"%PDF-1.7", headers={"Content-Type": "text/plain"}
    )
    assert resp.status_code == 415
    resp = client.put(
        f"/api/uploads/{PDF_KEY}", content=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}
    )
    assert resp.status_code == 204
    assert (tmp_path / PDF_KEY).read_bytes() == b"%PDF-1.7"


def test_safe_methods_and_non_api_paths_are_untouched(local_env: None) -> None:
    client = TestClient(create_app())
    assert client.get("/livez").status_code == 200
    resp = client.post("/events", content=b"x", headers={"Content-Type": "text/plain"})
    assert resp.status_code == 404  # not an /api path: only the cleanup function routes it


def _prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "s" * 40)
    monkeypatch.setenv("OTP_HMAC_KEY", "o" * 40)
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")


def test_prod_has_no_api_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    _prod_env(monkeypatch)
    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path


def test_local_keeps_api_docs(local_env: None) -> None:
    client = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200, path
