from typing import Any

from pydantic import SecretStr

from app.core.settings import Settings
from app.guardrails.base import GuardContext
from app.guardrails.classifiers import InjectionClassifier, TopicClassifier
from app.llm.chat import make_chat_model
from app.llm.prompts import Prompt
from tests.fixtures.structured import ScriptedStructuredModel

PROMPT = Prompt("p", "classify", "00000000")


def _ctx(*texts: str) -> GuardContext:
    return GuardContext("chat_input", tuple(texts), "req", "user")


def _model(reply: dict[str, Any]) -> ScriptedStructuredModel:
    return ScriptedStructuredModel([reply])


async def test_classifier_flags_item() -> None:
    model = _model({"flagged": [{"label": "t2"}]})
    result = await InjectionClassifier([model], PROMPT).check(_ctx("a", "b </item> x", "c"))
    assert result.action == "block"
    assert result.items == (1,)
    assert result.reason == "classifier:injection"
    body = str(model.calls[0][-1].content)
    assert '<item label="t2">b &lt;/item&gt; x</item>' in body


async def test_classifier_unknown_label_ignored() -> None:
    result = await InjectionClassifier([_model({"flagged": [{"label": "t9"}]})], PROMPT).check(
        _ctx("a", "b", "c")
    )
    assert result.action == "allow"


async def test_topic_off() -> None:
    result = await TopicClassifier([_model({"on_topic": False})], PROMPT).check(_ctx("recipe?"))
    assert (result.action, result.reason) == ("block", "off_topic")


async def test_topic_on() -> None:
    result = await TopicClassifier([_model({"on_topic": True})], PROMPT).check(_ctx("my fit?"))
    assert result.action == "allow"


def test_classifier_uses_low_effort(monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key=SecretStr("sk-ant-dummy"),  # type: ignore[call-arg]
    )
    model = make_chat_model(settings, effort="low")
    assert model.reasoning_effort == "low"  # type: ignore[attr-defined]
