"""Context labelling (r1.., j1..), prompt formatting and citation extraction."""

import re
from collections.abc import Mapping, Sequence
from html import escape
from typing import Final

from app.retrieval.search import RetrievedChunk
from app.schemas.chat import Citation

REFUSAL_TEXT: Final = (
    "I can only answer from your resume and the job description, and I couldn't find that in them."
)

_LABEL_RE: Final = re.compile(r"\[([rj]\d+)\]")
_PREFIX: Final = {"resume": "r", "jd": "j"}


def label_context(chunks: Sequence[RetrievedChunk]) -> dict[str, RetrievedChunk]:
    """Label resume chunks r1.. and JD chunks j1.., numbered in the given order."""
    counts = dict.fromkeys(_PREFIX.values(), 0)
    labelled: dict[str, RetrievedChunk] = {}
    for chunk in chunks:
        prefix = _PREFIX[chunk.doc_type]
        counts[prefix] += 1
        labelled[f"{prefix}{counts[prefix]}"] = chunk
    return labelled


def format_context(labelled: Mapping[str, RetrievedChunk]) -> str:
    """One ``<chunk>`` element per chunk. Text and attributes are HTML-escaped, so document
    text can never open or close a ``<chunk>`` tag."""
    return "\n".join(
        f'<chunk label="{label}" doc="{escape(c.doc_type)}" section="{escape(c.section)}">'
        f"{escape(c.text, quote=False)}</chunk>"
        for label, c in labelled.items()
    )


def extract_citations(
    answer: str, labelled: Mapping[str, RetrievedChunk]
) -> tuple[list[Citation], list[str]]:
    """Valid citations (deduplicated, first-use order) and unknown labels (likewise)."""
    citations: list[Citation] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for label in _LABEL_RE.findall(answer):
        if label in seen:
            continue
        seen.add(label)
        chunk = labelled.get(label)
        if chunk is None:
            invalid.append(label)
            continue
        citations.append(
            Citation(
                label=label,
                chunk_id=chunk.chunk_id,
                doc_type=chunk.doc_type,
                section=chunk.section,
                text=chunk.text,
            )
        )
    return citations, invalid
