from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.core.settings import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def test_events_route_only_in_cleanup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/x")
    assert "/events" not in create_app().openapi()["paths"]

    monkeypatch.setenv("CLEANUP_MODE", "true")
    get_settings.cache_clear()
    assert "/events" in create_app().openapi()["paths"]


async def test_events_handler_purges_with_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/x")
    monkeypatch.setenv("CLEANUP_MODE", "true")
    monkeypatch.setenv("RETENTION_HOURS", "5")
    get_settings.cache_clear()
    calls: list[dict[str, Any]] = []

    async def fake_purge(sm: object, *, now: object, retention: timedelta) -> dict[str, int]:
        calls.append({"retention": retention})
        return {"documents": 2}

    monkeypatch.setattr("app.api.events.purge_expired", fake_purge)
    monkeypatch.setattr("app.api.events.get_sessionmaker", lambda: object())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as c:
        r = await c.post("/events", json={})
    assert r.status_code == 200
    assert r.json() == {"documents": 2}
    assert calls == [{"retention": timedelta(hours=5)}]
