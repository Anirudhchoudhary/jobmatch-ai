"""Deterministic match score. The model never chooses the score."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.errors import AppError
from app.schemas.match import SkillMatch

WEIGHTS = {"required": 3, "nice_to_have": 1}
CREDITS = {"matched": 1.0, "partial": 0.5, "missing": 0.0}


@dataclass(frozen=True)
class ScoreResult:
    score: int
    required_coverage: float
    nice_to_have_coverage: float | None


def _coverage(skills: Sequence[SkillMatch], importance: str) -> float | None:
    group = [s for s in skills if s.requirement.importance == importance]
    if not group:
        return None
    return sum(CREDITS[s.status] for s in group) / len(group)


def compute_score(skills: Sequence[SkillMatch]) -> ScoreResult:
    """score = round-half-up(100 * sum(weight*credit) / sum(weight)), so 62.5 -> 63."""
    if not skills:
        raise AppError(
            "no_requirements", "We couldn't find any requirements in the job description.", 422
        )
    earned = sum(WEIGHTS[s.requirement.importance] * CREDITS[s.status] for s in skills)
    total = sum(WEIGHTS[s.requirement.importance] for s in skills)
    score = int(
        (Decimal(100) * Decimal(str(earned)) / Decimal(total)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )
    required = _coverage(skills, "required")
    return ScoreResult(
        score=score,
        required_coverage=required if required is not None else 0.0,
        nice_to_have_coverage=_coverage(skills, "nice_to_have"),
    )
