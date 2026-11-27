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


def test_embed_dimensions_must_match_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("EMBED_DIMENSIONS", "512")
    with pytest.raises(ValidationError, match="EMBED_DIMENSIONS must be 1024"):
        get_settings()


def test_bedrock_guard_requires_numbered_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("GUARD_BEDROCK", "enforce")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    with pytest.raises(ValidationError, match="numbered BEDROCK_GUARDRAIL_VERSION"):
        get_settings()
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "draft")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="numbered BEDROCK_GUARDRAIL_VERSION"):
        get_settings()
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")
    get_settings.cache_clear()
    assert get_settings().guard_bedrock == "enforce"
    monkeypatch.setenv("GUARD_BEDROCK", "off")
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID")
    monkeypatch.delenv("BEDROCK_GUARDRAIL_VERSION")
    get_settings.cache_clear()
    assert get_settings().guard_bedrock == "off"
