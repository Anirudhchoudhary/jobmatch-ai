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


def _prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("ENV", "prod")


def test_prod_requires_auth_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _prod_env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    with pytest.raises(ValidationError, match="SESSION_SIGNING_KEY"):
        get_settings()
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="OTP_HMAC_KEY"):
        get_settings()
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    get_settings.cache_clear()
    assert get_settings().env == "prod"


def test_prod_rejects_console_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    _prod_env(monkeypatch)
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    with pytest.raises(ValidationError, match="console"):
        get_settings()


def test_resend_sender_requires_key_and_from(monkeypatch: pytest.MonkeyPatch) -> None:
    _prod_env(monkeypatch)
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    with pytest.raises(ValidationError, match="RESEND_API_KEY"):
        get_settings()


def test_s3_backend_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("BLOB_BACKEND", "s3")
    with pytest.raises(ValidationError, match="UPLOADS_BUCKET"):
        get_settings()
    monkeypatch.setenv("UPLOADS_BUCKET", "fake-bucket")
    get_settings.cache_clear()
    assert get_settings().blob_backend == "s3"


@pytest.mark.parametrize("name", ["SESSION_SIGNING_KEY", "OTP_HMAC_KEY"])
@pytest.mark.parametrize("value", ["", "short-key"])
def test_prod_rejects_empty_or_short_keys(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _prod_env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError, match=f"{name} must be at least 32"):
        get_settings()
