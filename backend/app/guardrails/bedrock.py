"""Bedrock Guardrails (ApplyGuardrail) provider behind the Guardrail interface."""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

from app.guardrails.base import Action, GuardContext, GuardrailResult

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient


def _has_prompt_attack(assessments: list[Any]) -> bool:
    return any(
        f.get("type") == "PROMPT_ATTACK" and f.get("detected")
        for a in assessments
        for f in a.get("contentPolicy", {}).get("filters", [])
    )


def _pii_entities(assessments: list[Any]) -> list[Any]:
    return [
        e
        for a in assessments
        for e in a.get("sensitiveInformationPolicy", {}).get("piiEntities", [])
        if e.get("detected")
    ]


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
        """Apply the guardrail to each text, one call at a time.

        Precedence across items: block > redact > flag > allow. Every intervention is
        reported: an intervened item that is not a prompt attack, blocked PII or
        redactable PII is flagged (``bedrock:intervened``). Items of a lower-precedence
        action are dropped from ``items`` only when a higher action wins; the higher
        action is what gets recorded.
        """
        start = time.perf_counter()
        attack: list[int] = []
        pii_blocked: list[int] = []
        redacted: list[int] = []
        flagged: list[int] = []
        replaced = list(ctx.texts)
        for i, text in enumerate(ctx.texts):
            resp = await asyncio.to_thread(self._apply, text)
            if resp.get("action") != "GUARDRAIL_INTERVENED":
                continue
            assessments = resp.get("assessments", [])
            pii = _pii_entities(assessments)
            outputs = resp.get("outputs", [])
            if _has_prompt_attack(assessments):
                attack.append(i)
            elif any(e.get("action") == "BLOCKED" for e in pii):
                pii_blocked.append(i)
            elif pii and outputs and "text" in outputs[0]:
                replaced[i] = outputs[0]["text"]
                redacted.append(i)
            else:
                flagged.append(i)
        latency_ms = int((time.perf_counter() - start) * 1000)

        def result(action: Action, reason: str, items: list[int]) -> GuardrailResult:
            return GuardrailResult(
                self.name,
                self.provider,
                action,
                reason,
                latency_ms,
                items=tuple(items),
                redacted=tuple(replaced) if action == "redact" else None,
            )

        if attack:
            return result("block", "bedrock:prompt_attack", sorted(attack + pii_blocked))
        if pii_blocked:
            return result("block", "bedrock:pii_blocked", pii_blocked)
        if redacted:
            return result("redact", "bedrock:pii", redacted)
        if flagged:
            return result("flag", "bedrock:intervened", flagged)
        return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
