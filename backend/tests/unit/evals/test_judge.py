"""Offline tests for the DeepEval judge. Skipped when the optional `evals` group is absent."""

import os
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from structlog.testing import capture_logs

pytest.importorskip("deepeval")

from evals import judge as judge_module  # noqa: E402
from evals.judge import JUDGE_METRICS, ClaudeJudge, judge_chats, make_judge  # noqa: E402
from evals.results import ChatResult  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class Verdict(BaseModel):
    score: int


class _StubRunnable:
    async def ainvoke(self, prompt: str) -> Verdict:
        return Verdict(score=1)


class _FakeModel:
    def with_structured_output(self, schema: type[BaseModel], method: str) -> _StubRunnable:
        assert schema is Verdict
        assert method == "json_schema"
        return _StubRunnable()

    async def ainvoke(self, prompt: str) -> Any:
        class _Msg:
            text = "plain answer"

        return _Msg()


def _chat(kind: str, *, refused: bool = False, blocked_by: str | None = None) -> ChatResult:
    return ChatResult(
        qa_id=f"{kind}-1",
        question="Which languages?",
        kind=kind,  # type: ignore[arg-type]
        expect_refusal=kind != "answerable",
        answer="Python [r1]",
        refused=refused,
        blocked_by=blocked_by,
        grounded=None,
        fired=[],
        retrieved=["Skills: Python"],
        supporting=["Python"],
        expected_answer="Python",
    )


def _stub_metric(*, fail: bool = False) -> type:
    class Metric:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["include_reason"] is False
            assert kwargs["async_mode"] is True
            self.score: float | None = None

        async def a_measure(self, test_case: Any, **kwargs: Any) -> float:
            assert test_case.input == "Which languages?"
            assert test_case.retrieval_context == ["Skills: Python"]
            if fail:
                raise RuntimeError("secret detail jane.doe@example.com")
            self.score = 0.9
            return self.score

    return Metric


def _patch_metrics(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
    for name in (
        "FaithfulnessMetric",
        "AnswerRelevancyMetric",
        "ContextualPrecisionMetric",
        "ContextualRecallMetric",
    ):
        monkeypatch.setattr(judge_module, name, _stub_metric(fail=fail))


async def test_judge_structured_output() -> None:
    judge = ClaudeJudge(_FakeModel())  # type: ignore[arg-type]
    out = await judge.a_generate("p", schema=Verdict)
    assert out == Verdict(score=1)
    assert await judge.a_generate("p") == "plain answer"


def test_generate_refuses_inside_running_loop() -> None:
    judge = ClaudeJudge(_FakeModel())  # type: ignore[arg-type]
    assert judge.generate("p", schema=Verdict) == Verdict(score=1)  # no loop: runs
    assert judge.get_model_name() == "claude-sonnet-5-5"


async def test_generate_raises_when_loop_running() -> None:
    judge = ClaudeJudge(_FakeModel())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        judge.generate("p")


async def test_judge_chats_skips_refused_and_non_answerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_metrics(monkeypatch)
    ok = _chat("answerable")
    refused = _chat("answerable", refused=True)
    injection = _chat("injection")
    await judge_chats([ok, refused, injection], ClaudeJudge(_FakeModel()))  # type: ignore[arg-type]
    assert set(ok.judge) == set(JUDGE_METRICS)
    assert all(v == 0.9 for v in ok.judge.values())
    assert refused.judge == {}
    assert injection.judge == {}


async def test_metric_failure_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_metrics(monkeypatch, fail=True)
    chat = _chat("answerable")
    with capture_logs() as logs:
        await judge_chats([chat], ClaudeJudge(_FakeModel()))  # type: ignore[arg-type]
    assert chat.judge == {}
    failed = [e for e in logs if e["event"] == "judge_metric_failed"]
    assert {e["metric"] for e in failed} == set(JUDGE_METRICS)
    assert all(e["error_type"] == "RuntimeError" for e in failed)
    assert "jane.doe" not in repr(logs)


def test_telemetry_opt_out_set() -> None:
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "YES"
    assert os.environ["DEEPEVAL_DISABLE_DOTENV"] == "1"


def test_make_judge_constructs_without_calling() -> None:
    from app.core.settings import Settings

    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key=SecretStr("dummy"),
        database_url="postgresql+psycopg://u:p@localhost/db",
    )
    judge = make_judge(settings)
    assert judge.get_model_name() == settings.strong_chat_model
