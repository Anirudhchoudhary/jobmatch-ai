from scripts.report import GuardStats, RouteStats, render_report


def test_render_report_tables() -> None:
    out = render_report(
        [RouteStats("/api/stream/chat", 3, 120.0, 340.0, 0.001, 0.003, 0.33)],
        [GuardStats("pii", "redact", 2)],
        spent_today=0.0123,
        cap=1.0,
        days=7,
    )
    assert "| /api/stream/chat | 3 |" in out
    assert "Spend today: $0.0123 of $1.00 cap" in out
    assert "| pii | redact | 2 |" in out
    assert "last 7 day(s)" in out


def test_render_report_empty() -> None:
    out = render_report([], [], spent_today=0.0, cap=None, days=1)
    assert "No requests recorded." in out
    assert "(no cap)" in out
