import re

import httpx
import pytest

from app.main import create_app

pytestmark = pytest.mark.integration


async def test_health_live() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert re.match(r"^\d+\.\d+", body["pgvector"])
