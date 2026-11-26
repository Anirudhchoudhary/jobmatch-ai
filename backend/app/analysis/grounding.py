"""Evidence grounding: a quote counts only if it appears in the cited resume chunk."""

import re
import unicodedata
from collections.abc import Mapping, Sequence

from app.retrieval.search import RetrievedChunk
from app.schemas.match import Evidence, MatchDraft, Requirement, SkillMatch

_WS = re.compile(r"\s+")


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
