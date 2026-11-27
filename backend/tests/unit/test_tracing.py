from typing import Any
from uuid import uuid4

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from pydantic import ValidationError

from app.core.request_id import request_id_var
from app.core.settings import Settings
from app.observability import tracing
from app.observability.tracing import init_tracing, mask_pii, trace_config
from app.schemas.chat import ChatDone
from tests.unit.test_chat_graph import CHUNK, FakeRetriever, fake_model, offline_guardrails, state

DB = "postgresql+asyncpg://u:p@localhost/db"


@pytest.fixture(autouse=True)
def _tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "_client", None)


def test_mask_pii_redacts_nested_strings() -> None:
    data = {"a": ["mail x@y.co", ("call 415-555-0132",)], "n": 3}
    assert mask_pii(data=data) == {"a": ["mail [EMAIL]", ("call [PHONE]",)], "n": 3}


def test_tracing_off_without_keys() -> None:
    assert init_tracing(Settings(database_url=DB)) is False  # type: ignore[arg-type]
    uid = uuid4()
    cfg = trace_config(name="chat", user_id=uid, tags=["chat"])
    assert "callbacks" not in cfg
    assert cfg["metadata"]["langfuse_user_id"] == str(uid)
    assert cfg["run_name"] == "chat"


def test_one_key_only_rejected() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(database_url=DB, langfuse_public_key="pk")  # type: ignore[arg-type]


def test_trace_config_carries_request_id_and_tags() -> None:
    token = request_id_var.set("rid12345")
    try:
        cfg = trace_config(name="analysis", user_id=uuid4(), tags=("analysis", "analyst:x"))
    finally:
        request_id_var.reset(token)
    assert cfg["metadata"]["langfuse_session_id"] == "rid12345"
    assert cfg["metadata"]["langfuse_tags"] == ["analysis", "analyst:x"]


def test_init_with_keys_passes_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(tracing, "Langfuse", Recorder)
    settings = Settings(
        database_url=DB,  # type: ignore[arg-type]
        langfuse_public_key="pk",  # type: ignore[arg-type]
        langfuse_secret_key="sk",  # type: ignore[arg-type]
        langfuse_host="https://lf.example",
    )
    assert init_tracing(settings) is True
    assert seen["mask"] is mask_pii
    assert seen["host"] == "https://lf.example"
    assert seen["public_key"] == "pk"
    assert seen["environment"] == settings.env


def test_init_failure_leaves_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> None:
        raise RuntimeError("nope")

    monkeypatch.setattr(tracing, "Langfuse", boom)
    settings = Settings(
        database_url=DB,  # type: ignore[arg-type]
        langfuse_public_key="pk",  # type: ignore[arg-type]
        langfuse_secret_key="sk",  # type: ignore[arg-type]
    )
    assert init_tracing(settings) is False
    assert "callbacks" not in trace_config(name="x", user_id=uuid4(), tags=[])


async def test_handler_failure_does_not_break_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.graphs.chat import build_chat_graph, chat_events
    from app.llm.prompts import load_prompt

    calls: list[int] = []

    class Exploding(BaseCallbackHandler):
        def on_chain_start(self, *a: Any, **kw: Any) -> None:
            calls.append(1)
            raise RuntimeError("handler boom")

    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "CallbackHandler", Exploding)
    prompt = load_prompt("chat_system")
    graph = build_chat_graph(
        FakeRetriever([CHUNK]),  # type: ignore[arg-type]
        fake_model("Alex used Kafka [r1]."),
        prompt,
        min_relevance=0.2,
        guardrails=offline_guardrails(),
    )
    events = [e async for e in chat_events(graph, state(), prompt_version=prompt.version)]
    assert calls  # the handler really ran and raised
    assert isinstance(events[-1][1], ChatDone)
    assert events[-1][1].answer == "Alex used Kafka [r1]."
