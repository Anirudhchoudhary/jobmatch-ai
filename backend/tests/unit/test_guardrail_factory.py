from typing import Any

import pytest

from app.core.settings import Settings
from app.guardrails.factory import build_guardrails
from app.guardrails.pipeline import GuardrailPipeline, NullRecorder
from tests.fixtures.structured import ladder


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    for key, value in env.items():
        monkeypatch.setenv(key.upper(), value)
    return Settings()  # type: ignore[call-arg]


def _names(pipeline: GuardrailPipeline) -> list[str]:
    return [s.guard.name for s in pipeline.steps]


def test_offline_has_only_rules_and_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    g = build_guardrails(_settings(monkeypatch), NullRecorder(), models=None, bedrock_client=None)
    assert _names(g.upload) == ["injection_rules"]
    assert _names(g.chat_input) == ["pii", "injection_rules"]
    assert _names(g.output) == ["groundedness"]
    assert g.output.steps[0].guard.provider == "self"


def test_claude_guards_present_with_models(monkeypatch: pytest.MonkeyPatch) -> None:
    g = build_guardrails(
        _settings(monkeypatch, guard_topic="monitor"),
        NullRecorder(),
        models=ladder(),
        bedrock_client=None,
    )
    assert _names(g.upload) == ["injection_rules", "injection_classifier"]
    assert _names(g.chat_input) == [
        "pii",
        "injection_rules",
        "injection_classifier",
        "topic",
    ]
    assert [(s.guard.name, s.guard.provider) for s in g.output.steps] == [
        ("groundedness", "anthropic")
    ]
    modes = {s.guard.name: s.mode for s in g.chat_input.steps}
    assert modes["topic"] == "monitor"
    assert modes["injection_rules"] == "enforce"


def test_off_omits_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    g = build_guardrails(
        _settings(monkeypatch, guard_pii="off", guard_injection_rules="off"),
        NullRecorder(),
        models=None,
        bedrock_client=None,
    )
    assert g.upload.steps == ()
    assert g.chat_input.steps == ()
    off = _settings(monkeypatch, guard_groundedness="off")
    assert (
        build_guardrails(off, NullRecorder(), models=None, bedrock_client=None).output.steps == ()
    )


def test_bedrock_steps_present_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client: Any = object()
    g = build_guardrails(
        _settings(
            monkeypatch,
            guard_bedrock="enforce",
            bedrock_guardrail_id="gr-123",
            bedrock_guardrail_version="1",
        ),
        NullRecorder(),
        models=None,
        bedrock_client=client,
    )
    assert _names(g.upload) == ["injection_rules", "bedrock_guardrails"]
    assert _names(g.chat_input) == ["pii", "injection_rules", "bedrock_guardrails"]
    assert g.upload.steps[-1].mode == "enforce"
