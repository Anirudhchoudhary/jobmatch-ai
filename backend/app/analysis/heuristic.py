"""Offline, deterministic analyst: line-based extraction and keyword matching, no model calls."""

import re
from collections.abc import Mapping, Sequence

from app.analysis.score import ANALYSIS_VERSION
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    EvidenceDraft,
    MatchDraft,
    MatchStatus,
    Requirement,
    RequirementDraft,
    SkillDraft,
    SuggestionDraft,
)

_REQ_SECTIONS = ("Requirements", "Nice to have")
_SKIP_SECTIONS = frozenset({"About", "Benefits"})
_MAX_REQUIREMENTS = 30
# The brief's cut points, plus " or similar" so "Kafka or similar streaming systems" -> "Kafka".
_SKILL_CUT = re.compile(r",|\(|;| - | or similar\b", re.IGNORECASE)
_FILLER = re.compile(
    r"^(?:hands-on experience with|experience with|deep knowledge of|knowledge of"
    r"|familiarity with|proficiency in|strong|solid|\d+\+?\s*years?(?:\s+of)?)\s+",
    re.IGNORECASE,
)
_BULLET_CHARS = "-*•"
_MAX_SUGGESTIONS = 5
_WORD = re.compile(r"[a-z0-9+#]+")
_STOPWORDS = frozenset(
    {
        "and", "the", "for", "with", "experience", "knowledge", "strong", "deep", "years",
        "familiarity", "skills", "skill", "ability", "plus", "such", "similar", "using", "other",
        "least", "proven", "solid", "good", "working", "excellent", "understanding", "related",
    }
)  # fmt: skip
OFFLINE_RATIONALE = "Offline mode: rewriting needs a real model."


def _lines(text: str) -> list[str]:
    return [s for line in text.split("\n") if (s := line.strip())]


def _skill(line: str) -> str:
    """Cut at the first separator, drop trailing punctuation and leading filler phrases."""
    match = _SKILL_CUT.search(line)
    skill = (line[: match.start()] if match else line).strip().rstrip(".!?:;").strip()
    while (stripped := _FILLER.sub("", skill, count=1)) != skill:
        skill = stripped
    return skill[:80].strip()


def _bullet_line(text: str) -> str | None:
    """The first bullet line (marker stripped), else the first non-heading line."""
    lines = [line for line in _lines(text) if not line.startswith("#")]
    bullet = next((line for line in lines if line[0] in _BULLET_CHARS), None)
    if bullet is not None:
        return bullet.lstrip(_BULLET_CHARS).strip() or None
    return lines[0] if lines else None


def _tokens(skill: str) -> list[str]:
    return [w for w in _WORD.findall(skill.lower()) if len(w) >= 3 and w not in _STOPWORDS]


def _has_phrase(phrase: str, line: str) -> bool:
    pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
    return re.search(pattern, line, re.IGNORECASE) is not None


class HeuristicAnalyst:
    name = f"heuristic:v1:a{ANALYSIS_VERSION}"

    async def extract_requirements(
        self, jd: Mapping[str, RetrievedChunk]
    ) -> list[RequirementDraft]:
        chunks = [(label, c) for label, c in jd.items() if c.doc_type == "jd"]
        selected = [(label, c) for label, c in chunks if c.section in _REQ_SECTIONS]
        if not selected:
            selected = [(label, c) for label, c in chunks if c.section not in _SKIP_SECTIONS]
        out: list[RequirementDraft] = []
        for label, chunk in selected:
            for raw in _lines(chunk.text):
                line = raw.lstrip(_BULLET_CHARS).strip()
                skill = _skill(line)
                if not skill:
                    continue
                importance = "nice_to_have" if chunk.section == "Nice to have" else "required"
                out.append(
                    RequirementDraft(
                        skill=skill, text=line[:300], importance=importance, source=label
                    )
                )
        return out[:_MAX_REQUIREMENTS]

    async def extract_skills(self, resume: Mapping[str, RetrievedChunk]) -> list[SkillDraft]:
        return []

    async def match(
        self,
        requirements: Sequence[RequirementDraft],
        skills: Sequence[SkillDraft],
        resume: Mapping[str, RetrievedChunk],
    ) -> list[MatchDraft]:
        lines = [
            (label, line)
            for label, c in resume.items()
            if c.doc_type == "resume"
            for line in _lines(c.text)
        ]
        out: list[MatchDraft] = []
        for i, req in enumerate(requirements):
            tokens = _tokens(req.skill)
            hit: tuple[str, str] | None = next(
                (x for x in lines if _has_phrase(req.skill, x[1])), None
            )
            status: MatchStatus = "matched"
            if hit is None and tokens:
                status = "partial"
                hit = next(
                    (
                        x
                        for x in lines
                        if 2 * len(set(tokens) & set(_WORD.findall(x[1].lower()))) >= len(tokens)
                    ),
                    None,
                )
            if hit is None:
                out.append(MatchDraft(requirement_index=i, status="missing", evidence=[]))
            else:
                evidence = [EvidenceDraft(source=hit[0], quote=hit[1][:300])]
                out.append(MatchDraft(requirement_index=i, status=status, evidence=evidence))
        return out

    async def rewrite(
        self, bullets: Mapping[str, RetrievedChunk], targets: Sequence[Requirement]
    ) -> list[SuggestionDraft]:
        out: list[SuggestionDraft] = []
        for label, chunk in bullets.items():
            if len(out) == _MAX_SUGGESTIONS:
                break
            line = _bullet_line(chunk.text)
            if line is None:
                continue
            original = line[:300]
            out.append(
                SuggestionDraft(
                    source=label,
                    original=original,
                    rewritten=original,
                    rationale=OFFLINE_RATIONALE,
                    target_skills=[],
                )
            )
        return out
