"""Bedrock Guardrails (ApplyGuardrail) provider behind the Guardrail interface."""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

from app.guardrails.base import GuardContext, GuardrailResult

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient


def _has_prompt_attack(assessments: list[Any]) -> bool:
    return any(
        f.get("type") == "PROMPT_ATTACK" and f.get("detected")
        for a in assessments
        for f in a.get("contentPolicy", {}).get("filters", [])
    )


def _has_pii(assessments: list[Any]) -> bool:
    return any(
        e.get("detected")
        for a in assessments
        for e in a.get("sensitiveInformationPolicy", {}).get("piiEntities", [])
    )


class BedrockGuard:
    name = "bedrock_guardrails"
    provider = "bedrock"

    def __init__(
        self,
        client: "BedrockRuntimeClient",
        guardrail_id: str,
        version: str,
        *,
        source: Literal["INPUT", "OUTPUT"],
    ) -> None:
        self._client = client
        self._guardrail_id = guardrail_id
        self._version = version
        self._source = source

    def _apply(self, text: str) -> dict[str, Any]:
        return dict(
            self._client.apply_guardrail(
                guardrailIdentifier=self._guardrail_id,
                guardrailVersion=self._version,
                source=self._source,
                content=[{"text": {"text": text}}],
                outputScope="INTERVENTIONS",
            )
        )

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        blocked: list[int] = []
        redacted: list[int] = []
        replaced = list(ctx.texts)
        for i, text in enumerate(ctx.texts):
            resp = await asyncio.to_thread(self._apply, text)
            if resp.get("action") != "GUARDRAIL_INTERVENED":
                continue
            assessments = resp.get("assessments", [])
            if _has_prompt_attack(assessments):
                blocked.append(i)
            elif _has_pii(assessments):
                outputs = resp.get("outputs", [])
                if outputs and "text" in outputs[0]:
                    replaced[i] = outputs[0]["text"]
                    redacted.append(i)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if blocked:
            return GuardrailResult(
                self.name, self.provider, "block", "bedrock:prompt_attack", latency_ms,
                items=tuple(blocked),
            )  # fmt: skip
        if redacted:
            return GuardrailResult(
                self.name, self.provider, "redact", "bedrock:pii", latency_ms,
                items=tuple(redacted), redacted=tuple(replaced),
            )  # fmt: skip
        return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
