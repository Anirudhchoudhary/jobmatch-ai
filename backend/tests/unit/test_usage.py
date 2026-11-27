import io
import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from structlog.testing import capture_logs

from app.core.settings import Settings
from app.guardrails.base import GuardContext, GuardrailOutcome
from app.guardrails.pipeline import record_outcome
from app.llm.embeddings import BedrockTitanEmbedder
from app.llm.pricing import llm_cost_usd
from app.observability.usage import USAGE_CALLBACK, current_usage, usage_scope

DEFAULTS = Settings(database_url="postgresql://x/y").model_prices  # type: ignore[arg-type]


def _result(details: dict[str, int] | None = None, **meta: Any) -> LLMResult:
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    if details:
        usage["input_token_details"] = details  # type: ignore[assignment]
    msg = AIMessage(
        content="x",
        usage_metadata=usage,  # type: ignore[arg-type]
        response_metadata={"model_name": "claude-haiku-5-5", **meta},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def test_llm_cost_haiku_with_cache() -> None:
    cost = llm_cost_usd(
        DEFAULTS,
        "claude-haiku-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read=400_000,
        cache_write=100_000,
    )
    assert cost == pytest.approx(0.5665)


def test_unknown_model_costs_zero_and_warns_once() -> None:
    kw = {"input_tokens": 10, "output_tokens": 10, "cache_read": 0, "cache_write": 0}
    with capture_logs() as logs:
        assert llm_cost_usd(DEFAULTS, "claude-new-9", **kw) == 0.0
        assert llm_cost_usd(DEFAULTS, "claude-new-9", **kw) == 0.0
    events = [e for e in logs if e["event"] == "unpriced_model"]
    assert len(events) == 1
    assert events[0]["model"] == "claude-new-9"


async def test_callback_records_usage_into_open_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    with usage_scope() as usage:
        await USAGE_CALLBACK.on_llm_end(_result({"cache_read": 50}), run_id=uuid4())
    assert usage.models == ["claude-haiku-5-5"]
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 50
    assert usage.cost_usd > 0


async def test_callback_sums_ttl_cache_write_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    details = {
        "cache_creation": 0,
        "ephemeral_5m_input_tokens": 30,
        "ephemeral_1h_input_tokens": 10,
    }
    with usage_scope() as usage:
        await USAGE_CALLBACK.on_llm_end(_result(details), run_id=uuid4())
    assert usage.cache_write_tokens == 40


async def test_callback_without_scope_is_noop() -> None:
    assert current_usage() is None
    await USAGE_CALLBACK.on_llm_end(_result({"cache_read": 50}), run_id=uuid4())


async def test_callback_survives_settings_failure() -> None:
    # No DATABASE_URL: get_settings() raises; the callback must log and skip.
    with usage_scope() as usage, capture_logs() as logs:
        await USAGE_CALLBACK.on_llm_end(_result(), run_id=uuid4())
    assert usage.input_tokens == 0
    failed = [e for e in logs if e["event"] == "usage_record_failed"]
    assert failed
    assert failed[0]["error_type"]


def test_add_cost_and_models_distinct() -> None:
    with usage_scope() as usage:
        usage.add_cost("bedrock:titan", 0.001)
        usage.add_cost("bedrock:titan", 0.001)
    assert usage.models == ["bedrock:titan"]
    assert usage.cost_usd == pytest.approx(0.002)


class _Client:
    def invoke_model(self, **_: Any) -> dict[str, Any]:
        body = {"embedding": [0.0] * 1024, "inputTextTokenCount": 1000}
        return {"body": io.BytesIO(json.dumps(body).encode())}


async def test_titan_embedder_records_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    embedder = BedrockTitanEmbedder(_Client(), "amazon.titan-embed-text-v2:0", 1024)
    with usage_scope() as usage:
        await embedder.embed_query("hello")
    assert usage.cost_usd == pytest.approx(0.00002)
    assert usage.models == ["bedrock:titan"]


async def test_titan_embedder_without_scope_is_noop() -> None:
    embedder = BedrockTitanEmbedder(_Client(), "amazon.titan-embed-text-v2:0", 1024)
    assert len(await embedder.embed_query("hello")) == 1024


class _Recorder:
    async def record(self, ctx: GuardContext, outcome: GuardrailOutcome) -> None:
        return None


def _outcome(action: Any) -> GuardrailOutcome:
    return GuardrailOutcome(
        guardrail="topic",
        provider="fake",
        stage="chat_input",
        action=action,
        mode="enforce",
        reason="r",
        latency_ms=1,
        items=[],
    )


async def test_guardrail_outcomes_noted_in_ledger() -> None:
    ctx = GuardContext("chat_input", ("hi",), "rid", None)
    await record_outcome(_Recorder(), ctx, _outcome("flag"))  # type: ignore[arg-type]
    with usage_scope() as usage:
        await record_outcome(_Recorder(), ctx, _outcome("allow"))  # type: ignore[arg-type]
        assert usage.guardrails == []
        await record_outcome(_Recorder(), ctx, _outcome("flag"))  # type: ignore[arg-type]
    assert usage.guardrails == ["topic:flag"]


def test_make_chat_model_attaches_usage_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.chat import make_chat_model

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    from app.core.settings import get_settings

    model = make_chat_model(get_settings())
    assert model.callbacks is not None
    assert USAGE_CALLBACK in model.callbacks  # type: ignore[operator]
