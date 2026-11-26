"""Evidence grounding and the suggestion guard: model output must be backed by the cited
resume chunk (quotes verbatim; rewrites add no new numbers, employers, tools or skills)."""

import re
import unicodedata
from collections.abc import Mapping, Sequence

from app.analysis.heuristic import _WORD, _tokens
from app.retrieval.search import RetrievedChunk
from app.schemas.match import (
    BulletSuggestion,
    Evidence,
    MatchDraft,
    Requirement,
    SkillMatch,
    SuggestionDraft,
)

_WS = re.compile(r"\s+")
# Numbers or money (spec regex) as whole tokens. The possessive ``*+`` stops backtracking, so
# "1.5x" or "3.5GB" never yields a partial "1" or "3"; such tokens are left to the term rule.
_NUMBER = re.compile(r"(?<![\w$.,])\$?\d[\d,.]*+[%kKmMbB]?(?!\w)")
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_TOKEN = re.compile(r"\S+")
_EDGE = re.compile(r"^[^\w+#$%]+|[^\w+#$%]+$")
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)
_SENTENCE_END = frozenset(".!?")
_MAX_TARGET_SKILL_CHARS = 80


def normalize(text: str) -> str:
    """NFKC, lowercase, whitespace runs collapsed to one space, stripped."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text).lower()).strip()


def quote_in_chunk(quote: str, chunk_text: str) -> bool:
    q = normalize(quote)
    return bool(q) and q in normalize(chunk_text)


def _grounded(draft: MatchDraft, labelled: Mapping[str, RetrievedChunk]) -> list[Evidence]:
    out: list[Evidence] = []
    for ev in draft.evidence:
        chunk = labelled.get(ev.source)
        if (
            chunk is not None
            and chunk.doc_type == "resume"
            and quote_in_chunk(ev.quote, chunk.text)
        ):
            out.append(Evidence(chunk_id=chunk.chunk_id, quote=ev.quote))
    return out


def validate_matches(
    drafts: Sequence[MatchDraft],
    requirements: Sequence[Requirement],
    labelled: Mapping[str, RetrievedChunk],
) -> tuple[list[SkillMatch], int]:
    """One SkillMatch per requirement; ungrounded matched/partial drafts become missing."""
    by_index: dict[int, MatchDraft] = {}
    for d in drafts:
        by_index.setdefault(d.requirement_index, d)
    results: list[SkillMatch] = []
    downgraded = 0
    for i, requirement in enumerate(requirements):
        draft = by_index.get(i)
        if draft is None or draft.status == "missing":
            results.append(SkillMatch(requirement=requirement, status="missing", evidence=[]))
            continue
        evidence = _grounded(draft, labelled)
        if not evidence:
            downgraded += 1
            results.append(SkillMatch(requirement=requirement, status="missing", evidence=[]))
        else:
            results.append(
                SkillMatch(requirement=requirement, status=draft.status, evidence=evidence)
            )
    return results, downgraded


def _is_term(token: str, sentence_start: bool) -> bool:
    if any(c.isdigit() or c in "+#" for c in token):
        return True
    if len(token) >= 2 and token.isupper():
        return True
    return token[0].isupper() and not sentence_start


def _candidates(text: str) -> set[str]:
    """Numbers/money plus "terms": tokens with a digit, ``+`` or ``#``; ALL-CAPS tokens of 2+
    chars; capitalized tokens that do not start a sentence. A sentence starts at the first
    token of the text or after a token ending in ``.``, ``!`` or ``?`` (not after newlines).
    Tokens are split on ``/`` and lose a possessive ``'s`` before the checks."""
    found = {m.group().rstrip(".,") for m in _NUMBER.finditer(text)}
    at_start = True
    for m in _TOKEN.finditer(text):
        raw = m.group()
        parts = [_POSSESSIVE.sub("", _EDGE.sub("", p)) for p in raw.split("/")]
        parts = [p for p in parts if p]
        for i, part in enumerate(parts):
            if _is_term(part, at_start and i == 0):
                found.add(part)
        if parts or raw[-1] in _SENTENCE_END:
            at_start = raw[-1] in _SENTENCE_END
    return found


def _whole_word_in(value: str, source: str) -> bool:
    pattern = rf"(?<![\w+#])(?<!\d[.,]){re.escape(value)}(?![\w+#]|[.,]\d)"
    return re.search(pattern, source, re.IGNORECASE) is not None


def unsupported_terms(rewritten: str, source: str) -> list[str]:
    """Numbers and terms in ``rewritten`` that ``source`` lacks (case-insensitive, whole word;
    commas inside numbers are ignored). Sorted and unique."""
    src = _DIGIT_COMMA.sub("", source)
    return sorted(
        c for c in _candidates(rewritten) if not _whole_word_in(_DIGIT_COMMA.sub("", c), src)
    )


def new_target_terms(rewritten: str, source: str, targets: Sequence[Requirement]) -> list[str]:
    """Tokens of the target requirements' skill names (3+ chars, not stopwords) that
    ``rewritten`` uses but ``source`` lacks (case-insensitive, whole word). Words are split
    on hyphens, slashes and punctuation, so "Kubernetes-orchestrated", "on kubernetes" and
    "Kubernetes's" all count. Sorted and unique."""
    wanted = {t for r in targets for t in _tokens(r.skill)}
    used = wanted & set(_WORD.findall(rewritten.lower()))
    return sorted(t for t in used if not _whole_word_in(t, source))


def _target_skills(names: Sequence[str], targets: Sequence[Requirement]) -> list[str]:
    allowed = {r.skill.strip().casefold() for r in targets}
    return [n[:_MAX_TARGET_SKILL_CHARS] for n in names if n.strip().casefold() in allowed]


def validate_suggestions(
    drafts: Sequence[SuggestionDraft],
    labelled: Mapping[str, RetrievedChunk],
    targets: Sequence[Requirement],
    *,
    guard: Sequence[Requirement] | None = None,
) -> tuple[list[BulletSuggestion], int]:
    """Keep drafts whose original is quoted from the cited resume chunk and whose rewrite adds
    nothing unsupported: no new numbers or terms, and no skill word of ``guard`` (default:
    ``targets``) the chunk lacks. Pass every requirement as ``guard`` so a missing skill
    cannot be written in. ``target_skills`` keeps only names of the targets. Every dropped
    draft is a rejection."""
    guard_terms = targets if guard is None else guard
    kept: list[BulletSuggestion] = []
    for d in drafts:
        chunk = labelled.get(d.source)
        if (
            chunk is not None
            and chunk.doc_type == "resume"
            and quote_in_chunk(d.original, chunk.text)
            and not unsupported_terms(d.rewritten, chunk.text)
            and not new_target_terms(d.rewritten, chunk.text, guard_terms)
        ):
            kept.append(
                BulletSuggestion(
                    original_chunk_id=chunk.chunk_id,
                    original=d.original,
                    rewritten=d.rewritten,
                    rationale=d.rationale,
                    target_skills=_target_skills(d.target_skills, targets),
                )
            )
    return kept, len(drafts) - len(kept)
