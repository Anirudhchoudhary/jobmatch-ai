from collections.abc import AsyncIterator

import anyio
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.settings import get_settings
from app.db.engine import make_engine
from app.db.models import User
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.recorder import DbRecorder

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine(get_settings().database_url.get_secret_value())
    yield eng
    await eng.dispose()


def _outcome() -> GuardrailOutcome:
    return GuardrailOutcome(
        guardrail="injection_rules",
        provider="rules",
        stage="chat_input",
        action="block",
        mode="enforce",
        reason="pattern:ignore_instructions",
        latency_ms=7,
        items=[0, 2],
    )


async def test_db_recorder_writes_row(engine: AsyncEngine) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        user = User()
        s.add(user)
        await s.commit()
    rid = f"req-{user.id}"
    try:
        ctx = GuardContext(stage="chat_input", texts=("x",), request_id=rid, user_id=user.id)
        await DbRecorder(sm).record(ctx, _outcome())
        async with sm() as s:
            row = (
                (
                    await s.execute(
                        text("SELECT * FROM guardrail_events WHERE request_id = :r"), {"r": rid}
                    )
                )
                .mappings()
                .one()
            )
        assert row["user_id"] == user.id
        assert (row["stage"], row["guardrail"], row["provider"]) == (
            "chat_input",
            "injection_rules",
            "rules",
        )
        assert (row["action"], row["mode"], row["reason"]) == (
            "block",
            "enforce",
            "pattern:ignore_instructions",
        )
        assert row["latency_ms"] == 7
        assert row["item_count"] == 2
        assert row["created_at"] is not None
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM guardrail_events WHERE request_id = :r"), {"r": rid})
            await s.execute(text("DELETE FROM users WHERE id = :i"), {"i": user.id})
            await s.commit()


async def test_db_recorder_survives_cancel(engine: AsyncEngine) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    rid = "req-cancelled"
    ctx = GuardContext(stage="chat_input", texts=("x",), request_id=rid, user_id=None)
    try:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await DbRecorder(sm).record(ctx, _outcome())
        async with sm() as s:
            n = (
                await s.execute(
                    text("SELECT count(*) FROM guardrail_events WHERE request_id = :r"),
                    {"r": rid},
                )
            ).scalar_one()
        assert n == 1
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM guardrail_events WHERE request_id = :r"), {"r": rid})
            await s.commit()


async def test_item_count_falls_back_to_text_count(engine: AsyncEngine) -> None:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    rid = "req-itemcount"
    ctx = GuardContext(stage="chat_input", texts=("a", "b", "c"), request_id=rid, user_id=None)
    outcome = _outcome().model_copy(update={"items": []})
    try:
        await DbRecorder(sm).record(ctx, outcome)
        async with sm() as s:
            n = (
                await s.execute(
                    text("SELECT item_count FROM guardrail_events WHERE request_id = :r"),
                    {"r": rid},
                )
            ).scalar_one()
        assert n == 3
    finally:
        async with sm() as s:
            await s.execute(text("DELETE FROM guardrail_events WHERE request_id = :r"), {"r": rid})
            await s.commit()


async def test_db_recorder_times_out_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    from app.guardrails import recorder as recorder_module

    class Hanging:
        def __call__(self) -> "Hanging":
            return self

        async def __aenter__(self) -> "Hanging":
            await anyio.sleep(30)
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(recorder_module, "_WRITE_TIMEOUT_S", 0.1)
    ctx = GuardContext(stage="chat_input", texts=("x",), request_id="req-timeout", user_id=None)
    with capture_logs() as logs:
        await DbRecorder(Hanging()).record(ctx, _outcome())  # type: ignore[arg-type]
    assert any(e["event"] == "guardrail_record_timeout" for e in logs)
