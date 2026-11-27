"""Match-analysis schemas: model drafts (labels only) and public outputs (UUIDs)."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

Importance = Literal["required", "nice_to_have"]
MatchStatus = Literal["matched", "partial", "missing"]
Label = Annotated[str, StringConstraints(pattern=r"^[rj]\d+$")]
Quote = Annotated[str, StringConstraints(min_length=1, max_length=300)]
SkillName = Annotated[str, StringConstraints(min_length=1, max_length=80)]


# --- Drafts: model input/output, chunks referenced by label ---


class EvidenceDraft(BaseModel):
    source: Label
    quote: Quote


class RequirementDraft(BaseModel):
    skill: SkillName
    text: Quote
    importance: Importance
    source: Label


class RequirementsOut(BaseModel):
    requirements: list[RequirementDraft] = Field(max_length=30)


class SkillDraft(BaseModel):
    skill: SkillName
    evidence: list[EvidenceDraft] = Field(max_length=5)


class SkillsOut(BaseModel):
    skills: list[SkillDraft] = Field(max_length=60)


class MatchDraft(BaseModel):
    requirement_index: int = Field(ge=0)
    status: MatchStatus
    evidence: list[EvidenceDraft] = Field(max_length=5)


class MatchesOut(BaseModel):
    matches: list[MatchDraft]


class SuggestionDraft(BaseModel):
    source: Label
    original: Quote
    rewritten: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    rationale: Quote
    target_skills: list[str] = Field(max_length=5)


class SuggestionsOut(BaseModel):
    suggestions: list[SuggestionDraft] = Field(max_length=5)


# --- Public outputs: chunks referenced by UUID ---


class Evidence(BaseModel):
    chunk_id: UUID
    quote: str


class Requirement(BaseModel):
    skill: str
    text: str
    importance: Importance
    jd_chunk_id: UUID


class SkillMatch(BaseModel):
    requirement: Requirement
    status: MatchStatus
    evidence: list[Evidence]


class MatchAnalysis(BaseModel):
    analysis_id: UUID
    score: int = Field(ge=0, le=100)
    required_coverage: float
    nice_to_have_coverage: float | None
    skills: list[SkillMatch]
    downgraded_count: int
    analyst: str
    cached: bool
    request_id: str


class BulletSuggestion(BaseModel):
    original_chunk_id: UUID
    original: str
    rewritten: str
    rationale: str
    target_skills: list[str]


class BulletSuggestions(BaseModel):
    analysis_id: UUID
    suggestions: list[BulletSuggestion]
    rejected_count: int
    analyst: str
    request_id: str


class AnalysisProgress(BaseModel):
    step: Literal[
        "loading",
        "extracting_requirements",
        "extracting_skills",
        "matching",
        "validating",
        "scoring",
    ]


class SuggestionProgress(BaseModel):
    step: Literal["loading", "selecting", "rewriting", "validating"]


class SuggestionRequest(BaseModel):
    analysis_id: UUID | None = None
