"""Chat model factory: offline fake by default, ChatAnthropic when configured."""

from collections.abc import Iterator
from itertools import count
from typing import Literal

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable

from app.core.settings import Settings
from app.observability.usage import USAGE_CALLBACK

type ChatRunnable = Runnable[LanguageModelInput, BaseMessage]

# Transient provider trouble: worth switching models. Anything else (bad request, auth) is not.
RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)

FAKE_ANSWER = "(fake model) I can't answer without a real model [r1]."


def _fake_replies() -> Iterator[AIMessage]:
    # A fresh message per call: langchain mutates ids/metadata onto the instance.
    return (AIMessage(content=FAKE_ANSWER) for _ in count())


def make_chat_model(
    settings: Settings,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    timeout_s: float | None = None,
    effort: Literal["low", "medium"] = "medium",
) -> BaseChatModel:
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:  # Settings validator already guarantees this
            raise ValueError("anthropic_api_key is required")
        return ChatAnthropic(
            model=model if model is not None else settings.chat_model,
            max_tokens=max_tokens if max_tokens is not None else settings.chat_max_tokens,
            timeout=timeout_s if timeout_s is not None else settings.llm_timeout_s,
            max_retries=2,
            api_key=settings.anthropic_api_key,
            effort=effort,
            callbacks=[USAGE_CALLBACK],
        )
    return GenericFakeChatModel(messages=_fake_replies())


def with_fallback(primary: BaseChatModel, fallback: BaseChatModel) -> ChatRunnable:
    return primary.with_fallbacks([fallback], exceptions_to_handle=RETRYABLE_ERRORS)
