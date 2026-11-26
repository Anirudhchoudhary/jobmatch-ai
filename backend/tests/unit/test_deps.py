from unittest.mock import MagicMock

import pytest

from app.api.deps import get_current_user_id
from app.core.errors import AppError


async def test_current_user_refuses_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example.com:5432/x")
    session = MagicMock()
    with pytest.raises(AppError) as exc:
        await get_current_user_id(session)
    assert exc.value.status == 401
    assert exc.value.code == "auth_required"
    assert session.mock_calls == []
