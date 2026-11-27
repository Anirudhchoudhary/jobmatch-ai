from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.deps import DEV_USER_ID, get_current_user_id
from app.core.errors import AppError
from app.core.settings import get_settings


async def test_current_user_refuses_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
    monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
    monkeypatch.setenv("EMAIL_SENDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example.com:5432/x")
    session = MagicMock()
    with pytest.raises(AppError) as exc:
        await get_current_user_id(session)
    assert exc.value.status == 401
    assert exc.value.code == "auth_required"
    assert session.mock_calls == []


async def test_dev_fallback_only_in_local_and_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example.com:5432/x")
    for env in ("dev", "prod"):
        monkeypatch.setenv("ENV", env)
        get_settings.cache_clear()
        if env == "prod":
            monkeypatch.setenv("SESSION_SIGNING_KEY", "fake-signing-key-0123456789-abcdefghij")
            monkeypatch.setenv("OTP_HMAC_KEY", "fake-otp-hmac-key-0123456789-abcdefghij")
            monkeypatch.setenv("EMAIL_SENDER", "resend")
            monkeypatch.setenv("RESEND_API_KEY", "fake-resend")
            monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
        session = MagicMock()
        with pytest.raises(AppError) as exc:
            await get_current_user_id(session, "garbage.token")
        assert exc.value.code == "auth_required"
    for env in ("local", "ci"):
        monkeypatch.setenv("ENV", env)
        get_settings.cache_clear()
        session = MagicMock(execute=AsyncMock(), commit=AsyncMock())
        assert await get_current_user_id(session) == DEV_USER_ID
