import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ingestion.pdf import parse_pdf
from evals.dataset import QAItem, load_cases, render_resume_pdf, score_band


def _case(case_id: str, *, ci: bool = False) -> dict:
    return {
        "id": case_id,
        "category": "strong_match",
        "ci": ci,
        "resume": "Alex Rivera\nPython developer",
        "jd": "Looking for a Python developer",
        "expected": {"matched": ["python"], "missing": [], "score_band": "strong"},
        "qa": [
            {
                "id": "q1",
                "question": "What language?",
                "kind": "answerable",
                "expected_answer": "Python",
                "supporting": ["Python developer"],
                "expect_refusal": False,
            }
        ],
    }


def _write(directory: Path, filename: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_score_band_edges():
    assert score_band(70) == "strong"
    assert score_band(69) == "partial"
    assert score_band(40) == "partial"
    assert score_band(39) == "poor"


def test_answerable_requires_answer_and_support():
    with pytest.raises(ValidationError):
        QAItem(id="q1", question="?", kind="answerable", expect_refusal=False)


def test_expect_refusal_must_match_kind():
    with pytest.raises(ValidationError):
        QAItem(id="q1", question="?", kind="injection", expect_refusal=False)
    with pytest.raises(ValidationError):
        QAItem(id="q1", question="?", kind="pii", expect_refusal=True)


def test_load_cases_sorted_and_ci_subset(tmp_path):
    _write(tmp_path, "b-01.json", _case("b-01", ci=True))
    _write(tmp_path, "a-01.json", _case("a-01"))
    assert [c.id for c in load_cases(tmp_path)] == ["a-01", "b-01"]
    assert [c.id for c in load_cases(tmp_path, subset="ci")] == ["b-01"]


def test_load_cases_rejects_mismatched_filename(tmp_path):
    _write(tmp_path, "x.json", _case("a-01"))
    with pytest.raises(ValueError, match=r"x\.json"):
        load_cases(tmp_path)


def test_render_resume_pdf_is_byte_stable_and_parseable():
    text = "Alex Rivera\nSkills: Python, SQL\nCafé ☃ snowman"
    first = render_resume_pdf(text)
    assert first == render_resume_pdf(text)
    parsed = parse_pdf(first, max_bytes=5_242_880, max_pages=10)
    assert "Skills: Python, SQL" in parsed.text
    assert "Café ? snowman" in parsed.text
