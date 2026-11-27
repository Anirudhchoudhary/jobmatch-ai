import pytest
from pydantic import ValidationError

from app.core import settings as settings_module
from app.core.settings import get_settings


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://u:fakepw@h/db"
    monkeypatch.setenv("DATABASE_URL", url)
    s = get_settings()
    assert s.database_url.get_secret_value() == url
    assert "fakepw" not in repr(s)


def test_settings_from_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_SOURCE", "aws")
    calls: list[tuple[str, str]] = []

    def fake_load(secret_id: str, *, region: str) -> dict[str, str]:
        calls.append((secret_id, region))
        return {"DATABASE_URL": "postgresql+psycopg://x:y@z/db"}

    monkeypatch.setattr(settings_module, "load_secret", fake_load)
    s = get_settings()
    assert s.database_url.get_secret_value() == "postgresql+psycopg://x:y@z/db"
    assert calls == [("jobmatch/dev", "us-west-2")]


def test_settings_missing_database_url() -> None:
    with pytest.raises(ValidationError):
        get_settings()


def test_validation_error_does_not_echo_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_SOURCE", "aws")
    monkeypatch.setattr(
        settings_module,
        "load_secret",
        lambda secret_id, *, region: {
            "OTHER_KEY": "fake-value-do-not-leak",
            "DB_CONNECT_TIMEOUT_S": "fake-timeout-do-not-leak",
        },
    )
    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "fake-value-do-not-leak" not in str(exc.value)
    assert "fake-timeout-do-not-leak" not in str(exc.value)
