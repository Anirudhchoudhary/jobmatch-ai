import time

import httpx
import pytest

from app.api.health import DbStatus, check_database, get_db_checker
from app.db.engine import make_engine
from app.main import create_app


def _client(checker: object | None = None) -> httpx.AsyncClient:
    app = create_app()
    if checker is not None:
        app.dependency_overrides[get_db_checker] = lambda: checker
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _fixed(status: DbStatus) -> object:
    async def checker() -> DbStatus:
        return status

    return checker


async def test_health_ok() -> None:
    async with _client(_fixed(DbStatus(db="ok", pgvector="0.8.0"))) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0", "db": "ok", "pgvector": "0.8.0"}


async def test_health_db_error_returns_503() -> None:
    async with _client(_fixed(DbStatus(db="error", pgvector="unknown"))) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


async def test_health_pgvector_missing() -> None:
    async with _client(_fixed(DbStatus(db="ok", pgvector="missing"))) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


async def test_health_db_down_returns_503_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = make_engine("postgresql+psycopg://u:secretpw@127.0.0.1:1/db", connect_timeout_s=1)

    async def checker() -> DbStatus:
        return await check_database(engine, 2)

    start = time.monotonic()
    async with _client(checker) as c:
        r = await c.get("/health")
    elapsed = time.monotonic() - start
    await engine.dispose()
    assert r.status_code == 503
    assert elapsed < 3
    assert "secretpw" not in r.text
