import anthropic
import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.core.errors import ProviderError
from app.llm.structured import StructuredOutputError, pick_start_index, run_structured
from tests.fixtures.structured import Reply, ScriptedStructuredModel


class Out(BaseModel):
    n: int


MSGS = [HumanMessage("go")]


async def test_first_attempt_success() -> None:
    m = ScriptedStructuredModel([{"n": 1}])
    r = await run_structured(Out, MSGS, models=[m], step="t")
    assert (r.value.n, r.model_index, r.attempts) == (1, 0, 1)


async def test_retry_feeds_back_errors() -> None:
    m = ScriptedStructuredModel([{"n": "x"}, {"n": 2}])
    r = await run_structured(Out, MSGS, models=[m], step="t")
    assert (r.value.n, r.attempts) == (2, 2)
    last = m.calls[1][-1]
    assert isinstance(last, HumanMessage)
    assert "invalid" in str(last.content)
    assert "\n- n: " in str(last.content)
    assert "no structured output" not in str(last.content)
    assert len(m.calls[1]) == len(MSGS) + 2


async def test_semantic_validate_retries() -> None:
    m = ScriptedStructuredModel([{"n": 0}, {"n": 3}])
    r = await run_structured(
        Out,
        MSGS,
        models=[m],
        step="t",
        validate=lambda o: [] if o.n > 0 else ["n must be positive"],
    )
    assert r.value.n == 3
    assert "n must be positive" in str(m.calls[1][-1].content)


async def test_escalates_then_raises_model_output_invalid() -> None:
    a = ScriptedStructuredModel([{"n": "x"}] * 2)
    b = ScriptedStructuredModel([{"n": "x"}] * 2)
    with pytest.raises(StructuredOutputError) as exc:
        await run_structured(Out, MSGS, models=[a, b], step="t")
    assert exc.value.code == "model_output_invalid"
    assert exc.value.status == 502
    assert (len(a.calls), len(b.calls)) == (2, 2)

    a = ScriptedStructuredModel([{"n": "x"}] * 2)
    b = ScriptedStructuredModel([{"n": 5}])
    r = await run_structured(Out, MSGS, models=[a, b], step="t")
    assert (r.value.n, r.model_index) == (5, 1)


async def test_start_index_skips_primary() -> None:
    a = ScriptedStructuredModel([])
    b = ScriptedStructuredModel([{"n": 1}])
    r = await run_structured(Out, MSGS, models=[a, b], step="t", start_index=1)
    assert len(a.calls) == 0
    assert r.model_index == 1


async def test_provider_error_not_retried() -> None:
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    m = ScriptedStructuredModel([err, {"n": 1}])
    with pytest.raises(ProviderError):
        await run_structured(Out, MSGS, models=[m], step="t")
    assert len(m.calls) == 1


def test_pick_start_index() -> None:
    assert pick_start_index([HumanMessage("x" * 30_000)], 24_000) == 1
    assert pick_start_index([HumanMessage("short")], 24_000) == 0


async def test_logs_have_no_content() -> None:
    m = ScriptedStructuredModel([{"n": 0}, {"n": 1}])
    with capture_logs() as logs:
        await run_structured(
            Out,
            MSGS,
            models=[m],
            step="t",
            validate=lambda o: ["SECRETQUOTE"] if o.n == 0 else [],
        )
    assert "SECRETQUOTE" in str(m.calls[1][-1].content)
    assert logs
    assert "SECRETQUOTE" not in str(logs)


async def test_parser_exception_text_not_in_feedback() -> None:
    class Leaky(ScriptedStructuredModel):
        def with_structured_output(self, schema, **kw):  # type: ignore[no-untyped-def,override]
            async def run(_: object) -> dict[str, object]:
                self.calls.append(list(_))  # type: ignore[call-overload]
                if len(self.calls) == 1:
                    return {
                        "raw": AIMessage("{}"),
                        "parsed": None,
                        "parsing_error": OutputParserException("SECRETCOMPLETION"),
                    }
                return {"raw": AIMessage("{}"), "parsed": Out(n=1), "parsing_error": None}

            return RunnableLambda(lambda _: {}, afunc=run)

    m = Leaky([])
    await run_structured(Out, MSGS, models=[m], step="t")
    assert "SECRETCOMPLETION" not in "".join(str(x.content) for x in m.calls[1])


TRUNCATED = Reply('{"n": ', {"stop_reason": "max_tokens"})


async def test_truncated_output_escalates_without_same_model_retry() -> None:
    a = ScriptedStructuredModel([TRUNCATED, {"n": 1}])
    b = ScriptedStructuredModel([{"n": 2}])
    with capture_logs() as logs:
        r = await run_structured(Out, MSGS, models=[a, b], step="t")
    assert (r.value.n, r.model_index, r.attempts) == (2, 1, 1)
    assert (len(a.calls), len(b.calls)) == (1, 1)
    feedback = b.calls[0][-1]
    assert isinstance(feedback, HumanMessage)
    assert "cut off" in feedback.text
    assert "be concise" in feedback.text
    assert b.calls[0][: len(MSGS)] == MSGS
    failed = [e for e in logs if e["event"] == "structured_attempt_failed"]
    assert [e["truncated"] for e in failed] == [True]


async def test_truncated_on_last_model_raises() -> None:
    a = ScriptedStructuredModel([TRUNCATED])
    b = ScriptedStructuredModel([TRUNCATED, {"n": 1}])
    with pytest.raises(StructuredOutputError):
        await run_structured(Out, MSGS, models=[a, b], step="t")
    assert (len(a.calls), len(b.calls)) == (1, 1)


async def test_untruncated_failure_logs_truncated_false() -> None:
    m = ScriptedStructuredModel([Reply({"n": "x"}, {"stop_reason": "end_turn"}), {"n": 1}])
    with capture_logs() as logs:
        await run_structured(Out, MSGS, models=[m], step="t")
    failed = [e for e in logs if e["event"] == "structured_attempt_failed"]
    assert [e["truncated"] for e in failed] == [False]
