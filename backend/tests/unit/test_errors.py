import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError, install_error_handlers
from app.core.request_id import RequestIdMiddleware


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    @app.get("/app-error")
    def app_error() -> None:
        raise AppError("too_large", "File too big", 413)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret sk-ant-xyz")

    @app.post("/num")
    def num(body: int) -> int:
        return body

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_envelope() -> None:
    resp = _client().get("/app-error")
    assert resp.status_code == 413
    assert resp.json()["error"] == {
        "code": "too_large",
        "message": "File too big",
        "request_id": resp.headers["x-request-id"],
    }


def test_unhandled_error_is_generic_500() -> None:
    resp = _client().get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert resp.json()["error"]["message"] == "Something went wrong."
    assert "sk-ant" not in resp.text
    assert resp.json()["error"]["request_id"] == resp.headers["x-request-id"]


def test_request_id_accepts_valid_and_replaces_invalid() -> None:
    client = _client()
    ok = client.get("/app-error", headers={"X-Request-ID": "abcd1234-ef"})
    assert ok.headers["x-request-id"] == "abcd1234-ef"
    bad = client.get("/app-error", headers={"X-Request-ID": "<script>"})
    assert re.fullmatch(r"[0-9a-f]{32}", bad.headers["x-request-id"])


def test_validation_error_does_not_echo_input() -> None:
    resp = _client().post("/num", json={"q": "a@b.io"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    assert "a@b.io" not in resp.text


def test_starlette_404_and_405_use_envelope() -> None:
    client = _client()
    missing = client.get("/nope")
    assert missing.status_code == 404
    assert missing.json()["error"] == {
        "code": "not_found",
        "message": "Not found.",
        "request_id": missing.headers["x-request-id"],
    }
    wrong = client.post("/app-error")
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "method_not_allowed"
    assert wrong.headers["allow"] == "GET"
