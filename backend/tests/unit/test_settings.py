from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import settings as settings_module
from app.core.settings import get_settings


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in ("DATABASE_URL", "SECRETS_SOURCE", "SECRET_ID", "AWS_REGION"):
        monkeypatch.delenv(key, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    monkeypatch.setattr(settings_module, "ENV_FILE", empty)
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


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
