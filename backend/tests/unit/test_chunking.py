import re

import pytest

from app.ingestion.chunking import ChunkDraft, chunk_jd, chunk_resume

RESUME = """Jane Doe
Backend engineer

SUMMARY
Engineer with 8 years of experience.

Work Experience:
Senior Engineer, Acme (2020-2024)
- Built things

Engineer, Beta (2016-2020)
- Built other things

Technical Skills
Python, SQL

Education
BSc Computer Science
"""


def _shared(a: str, b: str) -> int:
    for n in range(min(len(a), len(b)), 0, -1):
        if a.endswith(b[:n]):
            return n
    return 0


def test_resume_sections_detected() -> None:
    chunks = chunk_resume(RESUME)
    order: list[str] = []
    for c in chunks:
        if not order or order[-1] != c.section:
            order.append(c.section)
    assert order == ["Summary", "Experience", "Skills", "Education"]
    for c in chunks:
        first = c.text.splitlines()[0].lower().rstrip(":")
        assert first not in {"summary", "work experience", "technical skills", "education"}


def test_experience_one_chunk_per_role() -> None:
    exp = [c for c in chunk_resume(RESUME) if c.section == "Experience"]
    assert len(exp) == 2
    assert exp[0].text.startswith("Senior Engineer, Acme")
    assert exp[1].text.startswith("Engineer, Beta")


def _long_block() -> str:
    lines = [f"- Line {i:03d} " + "word " * 12 for i in range(60)]
    return "\n".join(s.strip() for s in lines)


def test_long_block_split_with_overlap() -> None:
    block = _long_block()
    assert len(block) > 3500
    chunks = chunk_resume("Experience\n" + block)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1600 for c in chunks)
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert 100 <= _shared(a.text, b.text) <= 250
    for line in block.splitlines():
        assert any(line in c.text for c in chunks)


def test_block_of_exactly_max_chars_is_one_chunk() -> None:
    block = "x" * 1000 + "\n" + "y" * 599
    assert len(block) == 1600
    chunks = chunk_resume("Experience\n" + block)
    assert [c.text for c in chunks] == [block]


def test_block_one_char_over_max_is_split() -> None:
    block = "x" * 1000 + "\n" + "y" * 600
    assert len(chunk_resume("Experience\n" + block)) == 2


def test_single_long_line_is_split_without_empty_chunks() -> None:
    sentence = "This is a sentence about work. "
    line = (sentence * 120).strip()
    chunks = chunk_resume("Experience\n" + line)
    assert len(chunks) > 1
    assert all(c.text and len(c.text) <= 1600 for c in chunks)
    assert chunks[0].text.endswith(".")


def test_unbroken_token_is_hard_split() -> None:
    chunks = chunk_jd("z" * 5000, max_chars=1600, overlap=200)
    assert len(chunks) >= 3
    assert all(0 < len(c.text) <= 1600 for c in chunks)


def test_small_max_chars_terminates() -> None:
    text = "alpha beta gamma. delta epsilon zeta.\n" * 20
    chunks = chunk_jd(text, max_chars=50, overlap=40)
    assert chunks and all(0 < len(c.text) <= 50 for c in chunks)


def test_preamble_is_summary() -> None:
    chunks = chunk_resume("Jane Doe\nEngineer\n\nSkills\nPython")
    assert chunks[0].section == "Summary"
    assert chunks[0].text.startswith("Jane Doe")


def test_jd_sections() -> None:
    jd = "Acme is hiring.\n\nWhat you'll do\nShip code\n\nMust have\nPython\n\nNice-to-have\nRust\n"
    chunks = chunk_jd(jd)
    assert [c.section for c in chunks] == [
        "About",
        "Responsibilities",
        "Requirements",
        "Nice to have",
    ]


def test_markdown_and_dash_headings() -> None:
    chunks = chunk_resume("## Skills\nPython\n\nEducation —\nBSc")
    assert [c.section for c in chunks] == ["Skills", "Education"]


def test_heading_only_matches_short_lines_and_curly_apostrophe() -> None:
    long_line = "Experience " + "with many things " * 5
    chunks = chunk_resume(long_line)
    assert [c.section for c in chunks] == ["Summary"]
    jd = chunk_jd("What you’ll need\nPython")
    assert jd[0].section == "Requirements"


def test_empty_sections_produce_no_chunks() -> None:
    chunks = chunk_resume("Skills\n\nEducation\nBSc")
    assert [c.section for c in chunks] == ["Education"]
    assert chunk_resume("") == []


@pytest.mark.parametrize("fn", [chunk_resume, chunk_jd])
def test_no_headings_single_default_chunk(fn) -> None:  # type: ignore[no-untyped-def]
    chunks = fn("Just some plain text without any headings at all.")
    assert len(chunks) >= 1
    assert chunks[0].section == ("Summary" if fn is chunk_resume else "About")


def test_ordinals_are_contiguous() -> None:
    chunks = chunk_resume(RESUME + "\nProjects\n" + _long_block())
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert isinstance(chunks[0], ChunkDraft)
    assert not any(re.fullmatch(r"\s*", c.text) for c in chunks)
