from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from app.guardrails.base import Action, GuardContext, GuardrailOutcome, GuardrailResult
from app.guardrails.pipeline import GuardrailPipeline, GuardStep, record_outcome
from tests.fixtures.guardrails import ListRecorder, RaisingGuard, StaticGuard


def _ctx(*texts: str) -> GuardContext:
    return GuardContext(
        stage="chat_input", texts=texts or ("hi",), request_id="r1", user_id=uuid4()
    )


def _res(
    action: Action,
    name: str = "g",
    reason: str = "r",
    items: tuple[int, ...] = (),
    redacted: tuple[str, ...] | None = None,
) -> GuardrailResult:
    return GuardrailResult(name, "fake", action, reason, 1, items, redacted)


async def test_allow_records_nothing() -> None:
    rec = ListRecorder()
    pipe = GuardrailPipeline("chat_input", [GuardStep(StaticGuard(_res("allow")), "enforce")], rec)
    out = await pipe.run(_ctx())
    assert out.blocked is False
    assert len(out.outcomes) == 1 and out.outcomes[0].action == "allow"
    assert rec.records == []


async def test_enforce_block_short_circuits() -> None:
    rec = ListRecorder()
    second = StaticGuard(_res("allow", "second"))
    pipe = GuardrailPipeline(
        "chat_input",
        [GuardStep(StaticGuard(_res("block")), "enforce"), GuardStep(second, "enforce")],
        rec,
    )
    out = await pipe.run(_ctx())
    assert out.blocked is True
    assert second.calls == []
    assert len(rec.records) == 1 and rec.records[0][1].mode == "enforce"


async def test_monitor_block_does_not_block() -> None:
    rec = ListRecorder()
    pipe = GuardrailPipeline("chat_input", [GuardStep(StaticGuard(_res("block")), "monitor")], rec)
    out = await pipe.run(_ctx("a"))
    assert out.blocked is False and out.texts == ("a",)
    assert rec.records[0][1].mode == "monitor" and rec.records[0][1].action == "block"


async def test_redact_replaces_texts_for_later_guards() -> None:
    nxt = StaticGuard(_res("allow", "next"))
    pipe = GuardrailPipeline(
        "chat_input",
        [
            GuardStep(StaticGuard(_res("redact", redacted=("[EMAIL] hi",))), "enforce"),
            GuardStep(nxt, "enforce"),
        ],
        ListRecorder(),
    )
    out = await pipe.run(_ctx("a@b.co hi"))
    assert nxt.calls[0].texts == ("[EMAIL] hi",)
    assert out.texts == ("[EMAIL] hi",)


async def test_monitor_redact_keeps_texts() -> None:
    rec = ListRecorder()
    pipe = GuardrailPipeline(
        "chat_input",
        [GuardStep(StaticGuard(_res("redact", redacted=("[EMAIL] hi",))), "monitor")],
        rec,
    )
    out = await pipe.run(_ctx("a@b.co hi"))
    assert out.texts == ("a@b.co hi",)
    assert len(rec.records) == 1


async def test_item_scoped_block() -> None:
    later = StaticGuard(_res("allow", "later"))
    pipe = GuardrailPipeline(
        "upload",
        [GuardStep(StaticGuard(_res("block", items=(1,))), "enforce"), GuardStep(later, "enforce")],
        ListRecorder(),
    )
    out = await pipe.run(_ctx("a", "b", "c"))
    assert out.blocked is False
    assert out.blocked_items == frozenset({1})
    assert len(later.calls) == 1


async def test_item_block_covering_all_items_stops_pipeline() -> None:
    later = StaticGuard(_res("allow", "later"))
    pipe = GuardrailPipeline(
        "chat_input",
        [GuardStep(StaticGuard(_res("block", items=(0,))), "enforce"), GuardStep(later, "enforce")],
        ListRecorder(),
    )
    out = await pipe.run(_ctx("only"))
    assert out.blocked is True
    assert later.calls == []


async def test_monitor_item_block_covering_all_items_does_not_stop() -> None:
    later = StaticGuard(_res("allow", "later"))
    pipe = GuardrailPipeline(
        "chat_input",
        [GuardStep(StaticGuard(_res("block", items=(0,))), "monitor"), GuardStep(later, "enforce")],
        ListRecorder(),
    )
    out = await pipe.run(_ctx("only"))
    assert out.blocked is False
    assert len(later.calls) == 1


async def test_guard_exception_is_recorded_and_allows() -> None:
    rec = ListRecorder()
    later = StaticGuard(_res("allow", "later"))
    pipe = GuardrailPipeline(
        "chat_input", [GuardStep(RaisingGuard(), "enforce"), GuardStep(later, "enforce")], rec
    )
    out = await pipe.run(_ctx())
    assert out.blocked is False and len(later.calls) == 1
    outcome = rec.records[0][1]
    assert outcome.action == "error" and outcome.reason == "guard_error:RuntimeError"


async def test_flag_sets_flagged() -> None:
    pipe = GuardrailPipeline(
        "output", [GuardStep(StaticGuard(_res("flag")), "enforce")], ListRecorder()
    )
    out = await pipe.run(_ctx())
    assert out.flagged is True and out.blocked is False


async def test_outcome_reason_is_redacted() -> None:
    rec = ListRecorder()
    pipe = GuardrailPipeline(
        "chat_input", [GuardStep(StaticGuard(_res("flag", reason="x a@b.co")), "enforce")], rec
    )
    await pipe.run(_ctx())
    assert rec.records[0][1].reason == "x [EMAIL]"


async def test_record_outcome_logs_and_records() -> None:
    rec = ListRecorder()
    outcome = _outcome_for("flag")
    with capture_logs() as logs:
        await record_outcome(rec, _ctx("a", "b"), outcome)
    assert rec.records[0][1] is outcome
    (fired,) = [e for e in logs if e["event"] == "guardrail_fired"]
    assert fired["item_count"] == 2 and fired["reason"] == "r"
    assert {"guardrail", "provider", "stage", "action", "mode"} <= fired.keys()


async def test_record_outcome_swallows_recorder_failure() -> None:
    class Failing:
        async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
            raise RuntimeError("secret a@b.co")

    with capture_logs() as logs:
        await record_outcome(Failing(), _ctx(), _outcome_for("block"))
    failed = next(e for e in logs if e["event"] == "guardrail_record_failed")
    assert failed["error_type"] == "RuntimeError"
    assert "a@b.co" not in str(logs)


def _mk(action: Action, items: list[int]) -> GuardrailOutcome:
    return GuardrailOutcome(
        guardrail="g",
        provider="fake",
        stage="chat_input",
        action=action,
        mode="enforce",
        reason="r",
        latency_ms=1,
        items=items,
    )


def _outcome_for(action: Action) -> GuardrailOutcome:
    return _mk(action, [])


@pytest.mark.parametrize("items,expected", [([0, 1, 2], 3), ([], 2)])
async def test_item_count_rule_is_shared(items: list[int], expected: int) -> None:
    outcome = _mk("flag", items)
    with capture_logs() as logs:
        await record_outcome(ListRecorder(), _ctx("a", "b"), outcome)
    assert logs[0]["item_count"] == expected
