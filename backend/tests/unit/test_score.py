from uuid import uuid4

import pytest

from app.analysis.score import compute_score
from app.core.errors import AppError
from app.schemas.match import Importance, MatchStatus, Requirement, SkillMatch


def sm(importance: Importance, status: MatchStatus) -> SkillMatch:
    req = Requirement(skill="s", text="t", importance=importance, jd_chunk_id=uuid4())
    return SkillMatch(requirement=req, status=status, evidence=[])


def test_score_weights() -> None:
    r = compute_score(
        [sm("required", "matched"), sm("required", "missing"), sm("nice_to_have", "partial")]
    )
    assert r.score == round(100 * (3 * 1 + 3 * 0 + 1 * 0.5) / 7) == 50
    assert r.required_coverage == 0.5
    assert r.nice_to_have_coverage == 0.5


def test_score_all_matched_100() -> None:
    r = compute_score([sm("required", "matched"), sm("nice_to_have", "matched")])
    assert r.score == 100


def test_score_all_missing_0() -> None:
    assert compute_score([sm("required", "missing"), sm("nice_to_have", "missing")]).score == 0


def test_only_required_has_none_nice() -> None:
    r = compute_score([sm("required", "matched")])
    assert r.nice_to_have_coverage is None
    assert r.required_coverage == 1.0


def test_half_up_rounding() -> None:
    # required partial (3*0.5) + nice matched (1) = 2.5 / 4 -> 62.5; half-up gives 63
    # (banker's rounding would give 62).
    r = compute_score([sm("required", "partial"), sm("nice_to_have", "matched")])
    assert r.score == 63


def test_empty_raises_no_requirements() -> None:
    with pytest.raises(AppError) as ei:
        compute_score([])
    assert ei.value.code == "no_requirements"
    assert ei.value.status == 422
