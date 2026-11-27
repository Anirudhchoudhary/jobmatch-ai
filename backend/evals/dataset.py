"""Golden eval dataset: schema, loader and deterministic resume PDF rendering."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal[
    "strong_match",
    "partial_match",
    "poor_match",
    "career_changer",
    "missing_information",
    "injection_resume",
    "injection_jd",
    "injection_chat",
    "off_topic_chat",
    "pii_heavy",
    "fabrication_request",
]
CATEGORIES: tuple[Category, ...] = (
    "strong_match",
    "partial_match",
    "poor_match",
    "career_changer",
    "missing_information",
    "injection_resume",
    "injection_jd",
    "injection_chat",
    "off_topic_chat",
    "pii_heavy",
    "fabrication_request",
)
QAKind = Literal["answerable", "unanswerable", "injection", "off_topic", "fabrication", "pii"]
ScoreBand = Literal["strong", "partial", "poor"]

_REFUSAL_KINDS: frozenset[str] = frozenset(
    {"unanswerable", "injection", "off_topic", "fabrication"}
)

DATASET_DIR = Path(__file__).resolve().parents[2] / "evals" / "dataset"
_PDF_CREATION_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def score_band(score: int) -> ScoreBand:
    if score >= 70:
        return "strong"
    if score >= 40:
        return "partial"
    return "poor"


class QAItem(BaseModel):
    id: str
    question: str
    kind: QAKind
    expected_answer: str | None = None
    # Substrings of the resume/JD text the answer must rest on; stands in for chunk ids,
    # which are generated at ingest time.
    supporting: list[str] = []
    expect_refusal: bool

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.kind == "answerable" and (not self.expected_answer or not self.supporting):
            raise ValueError("answerable items need expected_answer and at least one supporting")
        if self.expect_refusal != (self.kind in _REFUSAL_KINDS):
            raise ValueError(f"expect_refusal does not match kind {self.kind!r}")
        return self


class Expected(BaseModel):
    matched: list[str]
    missing: list[str]
    score_band: ScoreBand


class Attacks(BaseModel):
    resume_injection: bool = False
    jd_injection: bool = False


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z_]+-\d{2}$")
    category: Category
    ci: bool = False
    resume: str
    jd: str
    expected: Expected
    attacks: Attacks = Attacks()
    pii_values: list[str] = []  # seeded fictional PII to hunt for in logs
    qa: list[QAItem] = Field(min_length=1)
    notes: str = ""  # why the case is hard / known failing


def load_cases(
    directory: Path = DATASET_DIR, *, subset: Literal["ci", "full"] = "full"
) -> list[EvalCase]:
    # Cases live in <directory>/cases; a flat directory of case files is accepted too.
    cases_dir = directory / "cases" if (directory / "cases").is_dir() else directory
    cases: dict[str, EvalCase] = {}
    for path in sorted(cases_dir.glob("*.json")):
        case = EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        if path.name != f"{case.id}.json":
            raise ValueError(f"{path.name}: filename must match case id {case.id!r}")
        if case.id in cases:
            raise ValueError(f"{path.name}: duplicate case id {case.id!r}")
        cases[case.id] = case
    ordered = [cases[key] for key in sorted(cases)]
    return [c for c in ordered if c.ci] if subset == "ci" else ordered


def render_resume_pdf(text: str) -> bytes:
    """Render plain text to a byte-stable PDF (fixed creation date, Helvetica is Latin-1)."""
    pdf = FPDF(format="Letter")
    pdf.set_creation_date(_PDF_CREATION_DATE)
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for raw in text.splitlines():
        line = raw.encode("latin-1", errors="replace").decode("latin-1")
        pdf.multi_cell(0, 5.5, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())
