from typing import Any

import boto3
import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.core.settings import Settings, get_settings
from app.llm.embeddings import HashEmbedder
from app.llm.providers import get_providers
from app.llm.rerank import NoopReranker
from tests.unit import conftest

DB = "postgresql+psycopg://u:p@h/db"


async def test_defaults_are_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("boto3 client created")

    monkeypatch.setattr(boto3, "client", boom)
    get_providers.cache_clear()
    p = get_providers()
    assert isinstance(p.embedder, HashEmbedder)
    assert isinstance(p.reranker, NoopReranker)
    for _ in range(2):  # the fake must not exhaust
        msg = await p.chat_model.ainvoke([HumanMessage("hi")])
        assert msg.content == "(fake model) I can't answer without a real model [r1]."
    get_providers.cache_clear()


def test_anthropic_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ValidationError):
        get_settings()


def test_anthropic_key_not_in_error_or_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
    assert "sk-ant-test123" not in repr(get_settings())
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "sk-ant" not in str(exc.value)


def test_anthropic_chat_model_builds_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")
    from langchain_anthropic import ChatAnthropic

    from app.llm.chat import make_chat_model

    m = make_chat_model(get_settings())
    assert isinstance(m, ChatAnthropic)
    assert m.reasoning_effort == "medium"
    assert m.max_tokens == 1024


def test_env_isolation_covers_all_fields() -> None:
    cleared = set(conftest._ISOLATED_ENV_VARS)
    assert {name.upper() for name in Settings.model_fields} <= cleared


async def test_fake_chat_returns_fresh_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DB)
    from app.llm.chat import make_chat_model

    m = make_chat_model(get_settings())
    a = await m.ainvoke([HumanMessage("hi")])
    b = await m.ainvoke([HumanMessage("hi")])
    assert a is not b
    assert a.id != b.id
