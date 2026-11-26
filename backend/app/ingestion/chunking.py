"""Section-aware chunking for resumes and job descriptions. Pure functions, no I/O.

Sizes are in characters. Input is expected to be normalized text (see app.ingestion.pdf).
"""

import re
from dataclasses import dataclass

_MAX_HEADING_CHARS = 40

_RESUME_ALIASES: dict[str, tuple[str, ...]] = {
    "Summary": (
        "summary",
        "professional summary",
        "career summary",
        "profile",
        "professional profile",
        "objective",
        "career objective",
        "about me",
        "about",
    ),
    "Experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "career history",
        "relevant experience",
    ),
    "Education": ("education", "academic background", "education and training", "academics"),
    "Skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "skills and tools",
        "core competencies",
        "technologies",
    ),
    "Projects": ("projects", "personal projects", "selected projects", "side projects"),
    "Certifications": (
        "certifications",
        "certificates",
        "licenses and certifications",
        "certification",
    ),
}

_JD_ALIASES: dict[str, tuple[str, ...]] = {
    "About": (
        "about",
        "about us",
        "about the role",
        "about the job",
        "overview",
        "the role",
        "company overview",
        "who we are",
        "job description",
        "role overview",
    ),
    "Responsibilities": (
        "responsibilities",
        "key responsibilities",
        "what you'll do",
        "what you will do",
        "your responsibilities",
        "duties",
        "the work",
        "in this role you will",
    ),
    "Requirements": (
        "requirements",
        "qualifications",
        "what you'll need",
        "what you will need",
        "must have",
        "minimum qualifications",
        "basic qualifications",
        "required skills",
        "what we're looking for",
        "who you are",
        "required qualifications",
    ),
    "Nice to have": (
        "nice to have",
        "preferred",
        "bonus",
        "preferred qualifications",
        "bonus points",
        "preferred skills",
        "pluses",
        "nice to haves",
    ),
    "Benefits": (
        "benefits",
        "perks",
        "what we offer",
        "perks and benefits",
        "compensation and benefits",
        "why join us",
    ),
}


@dataclass(frozen=True)
class ChunkDraft:
    section: str
    ordinal: int
    text: str


def _norm_heading(line: str) -> str:
    s = line.strip().lstrip("#").strip()
    s = s.replace("’", "'").replace("‘", "'")
    s = s.rstrip(":—–-").strip()
    s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"\s*&\s*", " and ", s)
    return re.sub(r"\s+", " ", s).lower()


def _alias_lookup(table: dict[str, tuple[str, ...]]) -> dict[str, str]:
    return {_norm_heading(a): sec for sec, aliases in table.items() for a in aliases}


_RESUME_LOOKUP = _alias_lookup(_RESUME_ALIASES)
_JD_LOOKUP = _alias_lookup(_JD_ALIASES)


def _heading(line: str, lookup: dict[str, str]) -> str | None:
    if not line.strip() or len(line.strip()) > _MAX_HEADING_CHARS:
        return None
    return lookup.get(_norm_heading(line))


def _sections(text: str, lookup: dict[str, str], preamble: str) -> list[tuple[str, str]]:
    out: list[tuple[str, list[str]]] = [(preamble, [])]
    for line in text.split("\n"):
        sec = _heading(line, lookup)
        if sec is None:
            out[-1][1].append(line)
        else:
            out.append((sec, []))
    return [(s, "\n".join(ls).strip()) for s, ls in out if "\n".join(ls).strip()]


# A unit is (separator_before, text); a chunk is the concatenation of its units.
_Unit = tuple[str, str]


def _split_words(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    cur = ""
    for word in text.split(" "):
        while len(word) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(word[:max_chars])
            word = word[max_chars:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= max_chars:
            cur += " " + word
        else:
            pieces.append(cur)
            cur = word
    if cur:
        pieces.append(cur)
    return pieces


def _split_line(line: str, max_chars: int) -> list[str]:
    """Split an over-long line at sentence boundaries, falling back to words."""
    pieces: list[str] = []
    cur = ""
    for sentence in re.split(r"(?<=[.!?;])\s+", line):
        if len(sentence) > max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.extend(_split_words(sentence, max_chars))
        elif not cur:
            cur = sentence
        elif len(cur) + 1 + len(sentence) <= max_chars:
            cur += " " + sentence
        else:
            pieces.append(cur)
            cur = sentence
    if cur:
        pieces.append(cur)
    return [p for p in pieces if p]


def _units(block: str, max_chars: int) -> list[_Unit]:
    units: list[_Unit] = []
    for line in block.split("\n"):
        if len(line) <= max_chars:
            units.append(("\n", line))
            continue
        for i, piece in enumerate(_split_line(line, max_chars)):
            units.append(("\n" if i == 0 else " ", piece))
    return units


def _text(units: list[_Unit]) -> str:
    return "".join(sep + t for sep, t in units)[1:] if units else ""


def _overlap_tail(units: list[_Unit], overlap: int) -> list[_Unit]:
    """Trailing whole units totalling <= overlap chars; else a word-snapped tail of the last."""
    if overlap <= 0 or not units:
        return []
    tail: list[_Unit] = []
    total = 0
    for sep, t in reversed(units):
        if total + len(sep) + len(t) > overlap:
            break
        tail.insert(0, (sep, t))
        total += len(sep) + len(t)
    if tail:
        return tail
    last = units[-1][1]
    cut = last[-overlap:]
    space = cut.find(" ")
    if space != -1 and last[-overlap - 1 : -overlap] != " " and space + 1 < len(cut):
        cut = cut[space + 1 :]
    return [("\n", cut)] if cut.strip() else []


def _split_block(block: str, max_chars: int, overlap: int) -> list[str]:
    block = block.strip()
    if not block:
        return []
    if len(block) <= max_chars:
        return [block]
    overlap = min(max(overlap, 0), max_chars // 2)
    chunks: list[str] = []
    prefix: list[_Unit] = []
    cur: list[_Unit] = []
    size = 0  # length of _text(prefix + cur)

    def flush() -> None:
        nonlocal prefix, cur, size
        full = prefix + cur
        chunks.append(_text(full))
        prefix = _overlap_tail(full, overlap)
        cur = []
        size = len(_text(prefix))

    for unit in _units(block, max_chars):
        add = len(unit[0]) + len(unit[1]) if (prefix or cur) else len(unit[1])
        if size + add > max_chars and (cur or prefix):
            if cur:
                flush()
                add = len(unit[0]) + len(unit[1]) if prefix else len(unit[1])
            if size + add > max_chars:  # overlap does not leave room: drop it
                prefix, size = [], 0
                add = len(unit[1])
        cur.append(unit)
        size += add
    if cur:
        chunks.append(_text(prefix + cur))
    return [c for c in chunks if c.strip()]


def _blank_blocks(body: str) -> list[str]:
    return [b for b in re.split(r"\n\s*\n", body) if b.strip()]


def _build(
    sections: list[tuple[str, str]], per_block: set[str], max_chars: int, overlap: int
) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for name, body in sections:
        blocks = _blank_blocks(body) if name in per_block else [body]
        for block in blocks:
            for piece in _split_block(block, max_chars, overlap):
                drafts.append(ChunkDraft(section=name, ordinal=len(drafts), text=piece))
    return drafts


def chunk_resume(text: str, *, max_chars: int = 1600, overlap: int = 200) -> list[ChunkDraft]:
    sections = _sections(text, _RESUME_LOOKUP, "Summary")
    return _build(sections, {"Experience"}, max_chars, overlap)


def chunk_jd(text: str, *, max_chars: int = 1600, overlap: int = 200) -> list[ChunkDraft]:
    sections = _sections(text, _JD_LOOKUP, "About")
    return _build(sections, set(), max_chars, overlap)
