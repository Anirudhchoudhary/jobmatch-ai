"""Per-claim groundedness guard: every claim in an answer must be backed by a chunk it cites."""

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Final

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.analysis.heuristic import STOPWORDS
from app.graphs.citations import REFUSAL_TEXT
from app.guardrails.base import GuardContext, GuardrailResult
from app.llm.prompts import Prompt
from app.llm.structured import run_structured

_CITATION: Final = re.compile(r"\[([rj]\d+)\]")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")
_LEADING_CITATIONS: Final = re.compile(r"^(?:\s*\[[rj]\d+\])+")
_TOKEN: Final = re.compile(r"[a-z0-9]+")
_CLAIM_LABEL: Final = re.compile(r"c([0-9]+)")
_BULLET: Final = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_ABBREVIATIONS: Final = frozenset(
    {"e.g.", "i.e.", "etc.", "vs.", "approx.", "incl.", "dr.", "mr.", "ms.", "mrs.", "jr.", "sr."}
)
_INITIALS: Final = re.compile(r"(?:[a-z]\.)+")
_MIN_WORDS: Final = 4
_MIN_TOKEN_LEN: Final = 3


@dataclass(frozen=True)
class Claim:
    index: int
    text: str
    labels: tuple[str, ...]


class GroundednessVerdicts(BaseModel):
    unsupported: list[str] = Field(default_factory=list)


def _split_sentences(line: str) -> list[str]:
    """Split after . ! ? plus whitespace, except after an abbreviation or dotted initials."""
    pieces: list[str] = []
    carry = False
    for part in _SENTENCE_END.split(line):
        if carry:
            pieces[-1] = f"{pieces[-1]} {part}"
        else:
            pieces.append(part)
        last = part.split()[-1].lower() if part.split() else ""
        carry = last in _ABBREVIATIONS or _INITIALS.fullmatch(last) is not None
    return pieces


def split_claims(answer: str) -> list[Claim]:
    """Sentences with at least four words (citations excluded), 0-indexed after filtering."""
    sentences: list[tuple[str, list[str]]] = []
    raws = [
        piece
        for line in answer.splitlines()
        if (stripped := _BULLET.sub("", line).strip())
        for piece in _split_sentences(stripped)
    ]
    for raw in raws:
        lead = _LEADING_CITATIONS.match(raw)
        if lead and sentences:
            # A citation placed after the full stop belongs to the previous sentence.
            prev_text, prev_labels = sentences[-1]
            sentences[-1] = (
                f"{prev_text} {lead.group(0).strip()}",
                [*prev_labels, *_CITATION.findall(lead.group(0))],
            )
            raw = raw[lead.end() :]
        text = raw.strip()
        if text:
            sentences.append((text, _CITATION.findall(text)))
    claims: list[Claim] = []
    for text, labels in sentences:
        if len(_CITATION.sub("", text).split()) < _MIN_WORDS:
            continue
        claims.append(Claim(len(claims), text, tuple(dict.fromkeys(labels))))
    return claims


def _parse_sources(sources: Sequence[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in sources:
        label, _, text = entry.partition("\t")
        parsed[label] = text
    return parsed


def _evidence_labels(claim: Claim, sources: Mapping[str, str]) -> list[str]:
    """Source labels a claim is judged against: its cited ones, or every source if uncited.

    A claim that cites only unknown labels has no evidence (empty list)."""
    if not claim.labels:
        return list(sources)
    return [label for label in claim.labels if label in sources]


def _tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN.findall(text.lower()) if len(t) >= _MIN_TOKEN_LEN and t not in STOPWORDS
    }


def _result(
    name: str, provider: str, unsupported: Sequence[int], total: int, latency_ms: int
) -> GuardrailResult:
    if not unsupported:
        return GuardrailResult(name, provider, "allow", "", latency_ms)
    return GuardrailResult(
        name,
        provider,
        "flag",
        f"ungrounded:{len(unsupported)}/{total}",
        latency_ms,
        items=tuple(unsupported),
    )


def _claims_to_check(ctx: GuardContext) -> list[Claim]:
    answer = ctx.texts[0] if ctx.texts else ""
    if answer.strip() == REFUSAL_TEXT:
        return []
    return split_claims(answer)


class LexicalGroundedness:
    name = "groundedness"
    provider = "self"

    def __init__(self, min_overlap: float) -> None:
        self._min_overlap = min_overlap

    def _supported(self, claim: Claim, sources: Mapping[str, str]) -> bool:
        cited = [sources[label] for label in _evidence_labels(claim, sources)]
        if not cited:
            return False
        tokens = _tokens(_CITATION.sub("", claim.text))
        if not tokens:
            return True  # vacuous evidence: nothing to verify, but the citation is still required
        evidence = _tokens(" ".join(cited))
        return len(tokens & evidence) / len(tokens) >= self._min_overlap

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        claims = _claims_to_check(ctx)
        sources = _parse_sources(ctx.sources)
        unsupported = [c.index for c in claims if not self._supported(c, sources)]
        latency_ms = int((time.perf_counter() - start) * 1000)
        return _result(self.name, self.provider, unsupported, len(claims), latency_ms)


def _format_claims(claims: Sequence[Claim], sources: Mapping[str, str]) -> str:
    blocks = []
    for claim in claims:
        lines = [f'<claim label="c{claim.index + 1}">{escape(claim.text, quote=False)}</claim>']
        lines += [
            f'<source label="{label}">{escape(sources[label], quote=False)}</source>'
            for label in _evidence_labels(claim, sources)
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class ClaudeGroundedness:
    name = "groundedness"
    provider = "anthropic"

    def __init__(self, models: Sequence[BaseChatModel], prompt: Prompt) -> None:
        self._models = models
        self._prompt = prompt

    async def check(self, ctx: GuardContext) -> GuardrailResult:
        start = time.perf_counter()
        claims = _claims_to_check(ctx)
        sources = _parse_sources(ctx.sources)
        unsupported = {c.index for c in claims if not _evidence_labels(c, sources)}
        askable = [c for c in claims if c.index not in unsupported]
        if askable:
            result = await run_structured(
                GroundednessVerdicts,
                [
                    SystemMessage(content=self._prompt.text),
                    HumanMessage(content=_format_claims(askable, sources)),
                ],
                models=self._models,
                step="guard_groundedness",
                max_attempts_per_model=1,
            )
            asked = {c.index for c in askable}
            for label in result.value.unsupported:
                match = _CLAIM_LABEL.fullmatch(label)
                if match and (index := int(match.group(1)) - 1) in asked:
                    unsupported.add(index)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return _result(self.name, self.provider, sorted(unsupported), len(claims), latency_ms)
