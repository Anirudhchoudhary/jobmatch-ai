"""Chat model factory: offline fake by default, ChatAnthropic when configured."""

from collections.abc import Iterator
from itertools import repeat

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.core.settings import Settings

FAKE_ANSWER = "(fake model) I can't answer without a real model [r1]."


def _fake_replies() -> Iterator[AIMessage]:
    return repeat(AIMessage(content=FAKE_ANSWER))


def make_chat_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:  # Settings validator already guarantees this
            raise ValueError("anthropic_api_key is required")
        return ChatAnthropic(
            model=settings.chat_model,
            max_tokens=settings.chat_max_tokens,
            timeout=settings.llm_timeout_s,
            max_retries=2,
            api_key=settings.anthropic_api_key,
            effort="medium",
        )
    return GenericFakeChatModel(messages=_fake_replies())
