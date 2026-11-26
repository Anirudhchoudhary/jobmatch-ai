"""Builds the per-stage guardrail pipelines from settings."""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from langchain_core.language_models import BaseChatModel

from app.core.settings import GuardMode, Settings, get_settings
from app.db.engine import get_sessionmaker
from app.guardrails.base import Guardrail, Stage
from app.guardrails.bedrock import BedrockGuard
from app.guardrails.classifiers import InjectionClassifier, TopicClassifier
from app.guardrails.pii import PiiGuard
from app.guardrails.pipeline import GuardrailPipeline, GuardrailRecorder, GuardStep
from app.guardrails.recorder import DbRecorder
from app.guardrails.rules import InjectionRules
from app.llm.chat import make_chat_model
from app.llm.prompts import load_prompt


@dataclass(frozen=True)
class Guardrails:
    upload: GuardrailPipeline
    chat_input: GuardrailPipeline
    output: GuardrailPipeline


def build_guardrails(
    settings: Settings,
    recorder: GuardrailRecorder,
    *,
    models: Sequence[BaseChatModel] | None,
    bedrock_client: Any | None,
) -> Guardrails:
    """A guard set to ``off``, or a Claude guard without ``models``, is omitted."""

    def step(mode: GuardMode, make: Any, *, needs: Any = True) -> list[GuardStep]:
        if mode == "off" or not needs:
            return []
        guard: Guardrail = make()
        return [GuardStep(guard, mode)]

    def bedrock() -> list[GuardStep]:
        guard_id, version = settings.bedrock_guardrail_id, settings.bedrock_guardrail_version
        if settings.guard_bedrock == "off" or bedrock_client is None:
            return []
        if guard_id is None or version is None:
            return []
        return [
            GuardStep(
                BedrockGuard(bedrock_client, guard_id, version, source="INPUT"),
                settings.guard_bedrock,
            )
        ]

    def injection() -> list[GuardStep]:
        return [
            *step(settings.guard_injection_rules, InjectionRules),
            *step(
                settings.guard_injection_classifier,
                lambda: InjectionClassifier(models or (), load_prompt("injection_classifier")),
                needs=models,
            ),
        ]

    def stage(name: Stage, steps: list[GuardStep]) -> GuardrailPipeline:
        return GuardrailPipeline(name, steps, recorder)

    topic = step(
        settings.guard_topic,
        lambda: TopicClassifier(models or (), load_prompt("topic_classifier")),
        needs=models,
    )
    return Guardrails(
        upload=stage("upload", [*injection(), *bedrock()]),
        chat_input=stage(
            "chat_input",
            [*injection(), *topic, *bedrock(), *step(settings.guard_pii, PiiGuard)],
        ),
        output=stage("output", []),
    )


@lru_cache
def get_guardrails() -> Guardrails:
    s = get_settings()
    models: list[BaseChatModel] | None = None
    if s.llm_provider == "anthropic":
        models = [
            make_chat_model(
                s,
                model=s.chat_model,
                max_tokens=s.guard_max_tokens,
                timeout_s=s.guard_timeout_s,
                effort="low",
            )
        ]
    client: Any | None = None
    if s.guard_bedrock != "off":
        config = Config(
            region_name=s.aws_region,
            read_timeout=s.bedrock_timeout_s,
            connect_timeout=3,
            retries={"mode": "adaptive", "max_attempts": 4},
        )
        client = boto3.client("bedrock-runtime", config=config)
    return build_guardrails(s, DbRecorder(get_sessionmaker()), models=models, bedrock_client=client)
