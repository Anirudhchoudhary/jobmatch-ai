"""Latency, cost and guardrail report over request_log and guardrail_events.

Usage: ``make report`` (``DAYS=30 make report`` for a longer window).
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.guardrails.spend import spent_today


@dataclass(frozen=True)
class RouteStats:
    route: str
    requests: int
    p50_ms: float
    p95_ms: float
    avg_cost_usd: float
    total_cost_usd: float
    cache_hit_rate: float


@dataclass(frozen=True)
class GuardStats:
    guardrail: str
    action: str
    count: int


_ROUTE_SQL = text(
    """
    SELECT route,
           count(*) AS requests,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
           coalesce(avg(cost_usd), 0) AS avg_cost,
           coalesce(sum(cost_usd), 0) AS total_cost,
           avg((cache_hit)::int) AS cache_hit_rate
    FROM request_log
    WHERE created_at >= :since
    GROUP BY route
    ORDER BY count(*) DESC, route
    """
)

_GUARD_SQL = text(
    """
    SELECT guardrail, action, count(*) AS n
    FROM guardrail_events
    WHERE created_at >= :since
    GROUP BY guardrail, action
    ORDER BY n DESC, guardrail, action
    """
)


async def fetch_route_stats(session: AsyncSession, *, since: datetime) -> list[RouteStats]:
    rows = (await session.execute(_ROUTE_SQL, {"since": since})).all()
    return [
        RouteStats(
            route=r.route,
            requests=int(r.requests),
            p50_ms=float(r.p50_ms),
            p95_ms=float(r.p95_ms),
            avg_cost_usd=float(r.avg_cost),
            total_cost_usd=float(r.total_cost),
            cache_hit_rate=float(r.cache_hit_rate or 0),
        )
        for r in rows
    ]


async def fetch_guard_stats(session: AsyncSession, *, since: datetime) -> list[GuardStats]:
    rows = (await session.execute(_GUARD_SQL, {"since": since})).all()
    return [GuardStats(guardrail=r.guardrail, action=r.action, count=int(r.n)) for r in rows]


def render_report(
    routes: list[RouteStats],
    guards: list[GuardStats],
    *,
    spent_today: float,
    cap: float | None,
    days: int,
) -> str:
    cap_text = f"of ${cap:.2f} cap" if cap is not None else "(no cap)"
    lines = [
        f"JobMatch report — last {days} day(s)",
        "",
        f"Spend today: ${spent_today:.4f} {cap_text}",
        "",
    ]
    if not routes:
        lines.append("No requests recorded.")
    else:
        lines += [
            "| route | requests | p50 ms | p95 ms | avg $ | total $ | cache hit % |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in routes:
            lines.append(
                f"| {r.route} | {r.requests} | {r.p50_ms:.0f} | {r.p95_ms:.0f} "
                f"| {r.avg_cost_usd:.4f} | {r.total_cost_usd:.4f} | {r.cache_hit_rate * 100:.0f} |"
            )
    lines.append("")
    if guards:
        lines += ["| guardrail | action | count |", "| --- | --- | --- |"]
        lines += [f"| {g.guardrail} | {g.action} | {g.count} |" for g in guards]
    else:
        lines.append("No guardrail events recorded.")
    return "\n".join(lines)


async def _run(days: int) -> str:
    settings = get_settings()
    since = datetime.now(UTC) - timedelta(days=days)
    async with get_sessionmaker()() as session:
        routes = await fetch_route_stats(session, since=since)
        guards = await fetch_guard_stats(session, since=since)
        spent = await spent_today(session)
    return render_report(
        routes, guards, spent_today=spent, cap=settings.daily_spend_cap_usd, days=days
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    print(asyncio.run(_run(args.days)))


if __name__ == "__main__":
    main()
