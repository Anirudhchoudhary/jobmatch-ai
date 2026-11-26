from uuid import uuid4

import pytest

from app.analysis.grounding import unsupported_terms, validate_suggestions
from app.retrieval.search import RetrievedChunk
from app.schemas.match import Requirement, SuggestionDraft

SOURCE = "Software Engineer, Acme Corp\nBuilt Kafka pipelines processing 2M events/day"
R1 = RetrievedChunk(uuid4(), uuid4(), "resume", "Experience", 0, SOURCE, 0.9)
J1 = RetrievedChunk(uuid4(), uuid4(), "jd", "Requirements", 0, "Kafka pipelines", 0.9)
LABELLED = {"r1": R1, "j1": J1}


def suggestion(
    source: str, original: str, rewritten: str, target_skills: list[str] | None = None
) -> SuggestionDraft:
    return SuggestionDraft(
        source=source,
        original=original,
        rewritten=rewritten,
        rationale="Leads with the impact.",
        target_skills=["Kafka"] if target_skills is None else target_skills,
    )


def target(skill: str) -> Requirement:
    return Requirement(skill=skill, text=skill, importance="required", jd_chunk_id=uuid4())


TARGETS = [target("Kafka"), target("Kubernetes")]


def test_unsupported_terms_flags_new_metric_employer_skill() -> None:
    rewritten = "Built Kafka pipelines at Google, cutting latency 40% using Kubernetes"
    assert unsupported_terms(rewritten, SOURCE) == ["40%", "Google", "Kubernetes"]


def test_unsupported_terms_allows_source_terms_and_sentence_start() -> None:
    rewritten = "Designed Kafka pipelines for Acme Corp handling 2M events/day"
    assert unsupported_terms(rewritten, SOURCE) == []


def test_unsupported_terms_details() -> None:
    src = "Cut costs by $1,000 using C++ and AWS. Led 3 engineers"
    # Commas inside numbers are ignored; sentence starts after '.' are exempt.
    assert unsupported_terms("Saved $1000 with C++ on AWS. Mentored 3 engineers.", src) == []
    # A number that only appears inside a larger one is not supported.
    assert unsupported_terms("Led 1 team", "Grew revenue 1.5x") == ["1"]
    # All-caps and symbol terms are flagged even at a sentence start.
    assert unsupported_terms("GCP and C# work", src) == ["C#", "GCP"]
    # Whole words only: "Acme" does not support "Acm".
    assert unsupported_terms("Worked at Acm", "Worked at Acme") == ["Acm"]
    # Only the text start and ".", "!" or "?" start a sentence: not bullets or newlines.
    bullets = "- Shipped features\n* Wrote tests"
    assert unsupported_terms(bullets, "shipped features, tests") == ["Wrote"]


def test_unsupported_terms_colon_and_semicolon_do_not_start_sentences() -> None:
    assert unsupported_terms("Stack: Kubernetes and Kafka", SOURCE) == ["Kubernetes"]
    assert unsupported_terms("Built pipelines; Terraform managed infra", SOURCE) == ["Terraform"]


def test_unsupported_terms_newline_does_not_start_sentence() -> None:
    assert unsupported_terms("Built Kafka pipelines\nGoogle scale", SOURCE) == ["Google"]


@pytest.mark.parametrize("value", ["1.5x", "3.5GB", "0.5s"])
def test_unsupported_terms_decimal_with_unit_copied_verbatim(value: str) -> None:
    text = f"Grew revenue {value}"
    assert unsupported_terms(text, text) == []


def test_unsupported_terms_new_decimal_with_unit_flagged() -> None:
    assert unsupported_terms("Grew revenue 2.5x", "Grew revenue 1.5x") == ["2.5x"]


def test_unsupported_terms_checks_slash_parts_and_possessives() -> None:
    assert unsupported_terms("Built kafka/Kubernetes pipelines", SOURCE) == ["Kubernetes"]
    assert unsupported_terms("AWS's Kafka pipelines", SOURCE) == ["AWS"]
    assert unsupported_terms("Ran Acme's Kafka pipelines", SOURCE) == []


def test_validate_suggestions_rejects_and_counts() -> None:
    drafts = [
        suggestion(
            "r1", "Built Kafka pipelines", "Designed Kafka pipelines handling 2M events/day"
        ),
        suggestion("r1", "Built Spark jobs", "Built Spark jobs"),
        suggestion("j1", "Kafka pipelines", "Kafka pipelines"),
    ]
    kept, rejected = validate_suggestions(drafts, LABELLED, TARGETS)
    assert rejected == 2
    assert len(kept) == 1
    assert kept[0].original_chunk_id == R1.chunk_id
    assert kept[0].rewritten == "Designed Kafka pipelines handling 2M events/day"
    assert kept[0].target_skills == ["Kafka"]


def test_validate_suggestions_rejects_unsupported_rewrite() -> None:
    drafts = [suggestion("r1", "Built Kafka pipelines", "Built Kafka pipelines at Google")]
    assert validate_suggestions(drafts, LABELLED, TARGETS) == ([], 1)


@pytest.mark.parametrize(
    "rewritten",
    [
        "Built Kubernetes-orchestrated Kafka pipelines",
        "Built Kafka pipelines (e.g. Kubernetes jobs)",
        "Built Kafka pipelines on kubernetes",
        "Built Kafka pipelines. Kubernetes managed them",
        "Built Kafka pipelines with Kubernetes's operators",
        "Built Kafka/kubernetes pipelines",
    ],
)
def test_validate_suggestions_rejects_new_target_skill(rewritten: str) -> None:
    drafts = [suggestion("r1", "Built Kafka pipelines", rewritten)]
    assert validate_suggestions(drafts, LABELLED, TARGETS) == ([], 1)


@pytest.mark.parametrize(
    "rewritten",
    ["Built Kafka pipelines! Google teams used them", "Built Kafka pipelines at google"],
)
def test_validate_suggestions_rejects_new_target_employer(rewritten: str) -> None:
    drafts = [suggestion("r1", "Built Kafka pipelines", rewritten)]
    assert validate_suggestions(drafts, LABELLED, [target("Google")]) == ([], 1)


def test_validate_suggestions_target_terms_in_source_are_allowed() -> None:
    src = "Ran kubernetes clusters for Kafka-based pipelines"
    labelled = {"r1": RetrievedChunk(uuid4(), uuid4(), "resume", "Experience", 0, src, 0.9)}
    drafts = [
        suggestion("r1", "Ran kubernetes clusters", "Ran Kubernetes clusters for Kafka pipelines")
    ]
    kept, rejected = validate_suggestions(drafts, labelled, TARGETS)
    assert (len(kept), rejected) == (1, 0)


def test_validate_suggestions_target_skills_limited_to_targets() -> None:
    long = "kafka" + "x" * 100
    drafts = [
        suggestion(
            "r1",
            "Built Kafka pipelines",
            "Designed Kafka pipelines",
            target_skills=["kafka", "Leadership", "KUBERNETES", long],
        )
    ]
    long_target = target(long)
    kept, _ = validate_suggestions(drafts, LABELLED, [*TARGETS, long_target])
    assert kept[0].target_skills == ["kafka", "KUBERNETES", long[:80]]
