"""CloudFront origin verification middleware."""

import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core import origin_verify
from app.core.errors import install_error_handlers
from app.core.origin_verify import OriginVerifyMiddleware
from app.core.request_id import RequestIdMiddleware
from app.core.settings import Settings

TOKEN = "t" * 40
DB = "postgresql+asyncpg://u:p@localhost/db"


def _client(token: str | None = TOKEN) -> TestClient:
    app = FastAPI()
    app.add_middleware(OriginVerifyMiddleware, token=token)
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    @app.get("/api/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"ok": "yes"}

    return TestClient(app)


def test_api_rejected_without_token_when_configured() -> None:
    res = _client().get("/api/ping")
    assert res.status_code == 403
    err = res.json()["error"]
    assert err["code"] == "forbidden"
    assert err["message"] == "Forbidden."
    assert err["request_id"] == res.headers["x-request-id"]


def test_api_allowed_with_token() -> None:
    res = _client().get("/api/ping", headers={"X-Origin-Verify": TOKEN})
    assert res.status_code == 200


def test_health_and_livez_open_with_token_configured() -> None:
    client = _client()
    assert client.get("/livez").status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("token", [None, ""])
def test_noop_when_token_unset(token: str | None) -> None:
    assert _client(token).get("/api/ping").status_code == 200


def test_wrong_token_is_403_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(origin_verify.hmac, "compare_digest", spy)
    res = _client().get("/api/ping", headers={"X-Origin-Verify": "wrong"})
    assert res.status_code == 403
    assert calls == [(b"wrong", TOKEN.encode())]


def test_rejection_logs_event_without_token() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        _client().get("/api/ping", headers={"X-Origin-Verify": "secret-guess"})
    assert [e["event"] for e in logs] == ["origin_verify_rejected"]
    assert "secret-guess" not in str(logs)
    assert TOKEN not in str(logs)


def test_settings_reject_empty_and_short_prod_token() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DB, origin_verify_token="")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=DB,
            env="prod",
            origin_verify_token="short",  # type: ignore[arg-type]
            session_signing_key="k" * 32,  # type: ignore[arg-type]
            otp_hmac_key="k" * 32,  # type: ignore[arg-type]
            email_sender="resend",
            resend_api_key="r",  # type: ignore[arg-type]
            email_from="a@b.c",
        )
