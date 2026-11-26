"""Prompt-injection pattern rules, applied to unicode-normalised text."""

import re
import time
import unicodedata

from app.guardrails.base import GuardContext, GuardrailResult

_ZERO_WIDTH = dict.fromkeys(
    map(
        ord,
        "\u200b\u200c\u200d\u2060\ufeff\u00ad\u200e\u200f"
        "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069",
    )
)


def normalize_for_rules(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower()
    return " ".join(text.split())


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx))
    for name, rx in (
        (
            "ignore_instructions",
            r"\b(ignore|disregard|forget)\b.{0,40}"
            r"\b(previous|prior|above|earlier|all|any|your)\b.{0,20}"
            r"\b(instructions?|prompts?|rules?|directions?)\b"
            r"|\boverride\b.{0,40}"
            r"\b(previous|prior|above|earlier|all|any|your)\b.{0,20}\b(instructions?|prompts?)\b",
        ),
        (
            "reveal_prompt",
            r"\b(reveal|show|print|repeat|output)\b.{0,30}"
            r"\b(system prompt|hidden prompt|your instructions|initial prompt)\b",
        ),
        (
            "role_override",
            r"\byou are now (an? )?(unfiltered|jailbroken|dan|evil|unrestricted)\b"
            r"|\bact as (an? )?(unfiltered|jailbroken|dan)\b"
            r"|\b(enable|enter|activate) (dan|jailbreak|developer) mode\b"
            r".{0,40}\b(no|without|ignore) (rules|restrictions|filters)\b",
        ),
        (
            "fake_tags",
            r"</?(system|assistant|context|instructions?)>|\[/?inst\]|<\|im_(start|end)\|>",
        ),
        (
            "score_override",
            r"\b(give|rate|score|mark)\b.{0,30}\b(candidate|resume|applicant)\b.{0,30}"
            r"(\b(100|perfect|highest) (score|rating|marks?)\b|\b100( out of 100|/100|%)"
            r"|\bfull marks\b|\b10 ?/ ?10\b)",
        ),
    )
)


def _first_match(text: str) -> str | None:
    normalized = normalize_for_rules(text)
    for name, pattern in _PATTERNS:
        if pattern.search(normalized):
            return name
    return None


class InjectionRules:
    name = "injection_rules"
    provider = "self"

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        hits = [(i, _first_match(t)) for i, t in enumerate(ctx.texts)]
        hits = [(i, n) for i, n in hits if n is not None]
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not hits:
            return GuardrailResult(self.name, self.provider, "allow", "", latency_ms)
        return GuardrailResult(
            self.name,
            self.provider,
            "block",
            f"pattern:{hits[0][1]}",
            latency_ms,
            items=tuple(i for i, _ in hits),
        )
