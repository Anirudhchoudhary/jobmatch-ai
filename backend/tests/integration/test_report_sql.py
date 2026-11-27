from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import RequestLog
from scripts.report import fetch_route_stats

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


async def test_fetch_route_stats_percentiles(engine: AsyncEngine) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    route = f"/test/report-{uuid4()}"
    async with sm() as s:
        s.add_all(
            RequestLog(
                request_id=str(uuid4()),
                route=route,
                method="POST",
                status=200,
                latency_ms=ms,
                cache_hit=hit,
            )
            for ms, hit in [(100, False), (200, True), (300, False), (400, False)]
        )
        await s.commit()
    try:
        async with sm() as s:
            stats = await fetch_route_stats(s, since=datetime.now(UTC) - timedelta(minutes=5))
        (row,) = [r for r in stats if r.route == route]
        assert row.requests == 4
        assert row.p50_ms == pytest.approx(250)
        assert row.p95_ms == pytest.approx(385)
        assert row.cache_hit_rate == pytest.approx(0.25)
    finally:
        async with sm() as s:
            await s.execute(delete(RequestLog).where(RequestLog.route == route))
            await s.commit()
