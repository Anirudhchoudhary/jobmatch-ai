"""Structural checks on the real golden dataset (evals/dataset)."""

from collections import Counter

import pytest

from app.core.pii import _EMAIL_RE, _PHONE_RE
from app.ingestion.pdf import parse_pdf
from evals.dataset import CATEGORIES, DATASET_DIR, load_cases, render_resume_pdf

CASES = load_cases()


def _texts(case):
    yield case.resume
    yield case.jd
    yield case.notes
    for item in case.qa:
        yield item.question
        yield item.expected_answer or ""
        yield from item.supporting


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


def test_at_least_30_cases_and_every_category():
    assert len(CASES) >= 30
    counts = Counter(c.category for c in CASES)
    for category in CATEGORIES:
        assert counts[category] >= 2, category


def test_ci_subset_one_per_category():
    ci = load_cases(subset="ci")
    assert len(ci) == 11
    assert len({c.category for c in ci}) == 11


def test_supporting_substrings_exist():
    for case in CASES:
        haystack = (case.resume + "\n" + case.jd).lower()
        for item in case.qa:
            for s in item.supporting:
                assert s.lower() in haystack, (case.id, item.id, s)


def test_qa_volume_and_mix():
    assert sum(len(c.qa) for c in CASES) >= 90
    for case in CASES:
        assert any(q.kind == "answerable" for q in case.qa), case.id


def test_document_sizes():
    for case in CASES:
        assert 150 <= len(case.resume.split()) <= 450, (case.id, "resume")
        assert 120 <= len(case.jd.split()) <= 300, (case.id, "jd")


def test_only_fictional_pii():
    for case in CASES:
        for text in _texts(case):
            for m in _EMAIL_RE.finditer(text):
                assert (
                    m.group().lower().endswith(("@example.com", "@example.org", "@example.net"))
                ), (case.id, m.group())
            for m in _PHONE_RE.finditer(text):
                digits = sum(ch.isdigit() for ch in m.group())
                if 9 <= digits <= 15:
                    assert "555-01" in m.group(), (case.id, m.group())
        for value in case.pii_values:
            assert value in case.resume, (case.id, value)


def test_attack_flags_match_category():
    for case in CASES:
        assert case.attacks.resume_injection == (case.category == "injection_resume"), case.id
        assert case.attacks.jd_injection == (case.category == "injection_jd"), case.id


def test_pdfs_in_sync():
    for case in CASES:
        path = DATASET_DIR / "pdfs" / f"{case.id}.pdf"
        assert render_resume_pdf(case.resume) == path.read_bytes(), case.id


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_resume_supporting_substrings_survive_pdf(case):
    parsed = parse_pdf(
        (DATASET_DIR / "pdfs" / f"{case.id}.pdf").read_bytes(), max_bytes=5_242_880, max_pages=10
    )
    resume, text = _squash(case.resume), _squash(parsed.text)
    for item in case.qa:
        for s in item.supporting:
            if _squash(s) in resume:
                assert _squash(s) in text, (case.id, item.id, s)
