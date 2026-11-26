from uuid import uuid4

from app.analysis.grounding import unsupported_terms, validate_suggestions
from app.retrieval.search import RetrievedChunk
from app.schemas.match import SuggestionDraft

SOURCE = "Software Engineer, Acme Corp\nBuilt Kafka pipelines processing 2M events/day"
R1 = RetrievedChunk(uuid4(), uuid4(), "resume", "Experience", 0, SOURCE, 0.9)
J1 = RetrievedChunk(uuid4(), uuid4(), "jd", "Requirements", 0, "Kafka pipelines", 0.9)
LABELLED = {"r1": R1, "j1": J1}


def suggestion(source: str, original: str, rewritten: str) -> SuggestionDraft:
    return SuggestionDraft(
        source=source,
        original=original,
        rewritten=rewritten,
        rationale="Leads with the impact.",
        target_skills=["Kafka"],
    )


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
    # Bullet markers start a sentence.
    assert (
        unsupported_terms("- Shipped features\n* Wrote tests", "shipped features, wrote tests")
        == []
    )


def test_validate_suggestions_rejects_and_counts() -> None:
    drafts = [
        suggestion(
            "r1", "Built Kafka pipelines", "Designed Kafka pipelines handling 2M events/day"
        ),
        suggestion("r1", "Built Spark jobs", "Built Spark jobs"),
        suggestion("j1", "Kafka pipelines", "Kafka pipelines"),
    ]
    kept, rejected = validate_suggestions(drafts, LABELLED)
    assert rejected == 2
    assert len(kept) == 1
    assert kept[0].original_chunk_id == R1.chunk_id
    assert kept[0].rewritten == "Designed Kafka pipelines handling 2M events/day"
    assert kept[0].target_skills == ["Kafka"]


def test_validate_suggestions_rejects_unsupported_rewrite() -> None:
    drafts = [suggestion("r1", "Built Kafka pipelines", "Built Kafka pipelines at Google")]
    assert validate_suggestions(drafts, LABELLED) == ([], 1)
