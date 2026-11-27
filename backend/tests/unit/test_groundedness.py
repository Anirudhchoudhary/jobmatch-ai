from app.graphs.citations import REFUSAL_TEXT
from app.guardrails.base import GuardContext
from app.guardrails.groundedness import ClaudeGroundedness, LexicalGroundedness, split_claims
from app.llm.prompts import Prompt
from tests.fixtures.structured import ScriptedStructuredModel

PROMPT = Prompt("p", "judge", "00000000")
SOURCES = (
    "r1\tBuilt Kafka pipelines at Acme for streaming analytics",
    "r2\tLed a team of five engineers",
)


def _ctx(answer: str, sources: tuple[str, ...] = SOURCES) -> GuardContext:
    return GuardContext("output", (answer,), "req", "user", sources)


def test_split_claims() -> None:
    claims = split_claims(
        "Built Kafka pipelines at Acme [r1]. Yes. Led a team of five engineers [r2][j1]!"
    )
    assert [(c.index, c.labels) for c in claims] == [(0, ("r1",)), (1, ("r2", "j1"))]


def test_split_claims_trailing_citation_after_period() -> None:
    claims = split_claims("Built Kafka pipelines at Acme. [r1] Led a team of five engineers.")
    assert [c.labels for c in claims] == [("r1",), ()]


async def test_lexical_supported() -> None:
    result = await LexicalGroundedness(0.5).check(_ctx("You built Kafka pipelines at Acme [r1]."))
    assert result.action == "allow"


async def test_lexical_no_citation() -> None:
    result = await LexicalGroundedness(0.5).check(_ctx("You built Kafka pipelines at Acme."))
    assert (result.action, result.reason, result.items) == ("flag", "ungrounded:1/1", (0,))


async def test_lexical_unknown_label() -> None:
    result = await LexicalGroundedness(0.5).check(_ctx("You built Kafka pipelines at Acme [r9]."))
    assert result.action == "flag"


async def test_lexical_words_absent() -> None:
    result = await LexicalGroundedness(0.5).check(
        _ctx("You managed Oracle databases across Europe [r1].")
    )
    assert result.action == "flag"


async def test_lexical_refusal_and_empty_allowed() -> None:
    guard = LexicalGroundedness(0.5)
    assert (await guard.check(_ctx(REFUSAL_TEXT))).action == "allow"
    assert (await guard.check(_ctx("Ok."))).action == "allow"


async def test_claude_judge_flags_claim() -> None:
    model = ScriptedStructuredModel([{"unsupported": ["c2", "c9"]}])
    answer = "You built Kafka pipelines at Acme [r1]. You led a team of five engineers [r2]."
    result = await ClaudeGroundedness([model], PROMPT).check(_ctx(answer))
    assert (result.action, result.reason, result.items) == ("flag", "ungrounded:1/2", (1,))
    assert '<claim label="c2">' in str(model.calls[0][-1].content)


async def test_claude_unlabelled_claims_skip_model() -> None:
    model = ScriptedStructuredModel([])
    result = await ClaudeGroundedness([model], PROMPT).check(
        _ctx("You built Kafka pipelines at Acme.")
    )
    assert (result.action, result.items) == ("flag", (0,))
    assert model.calls == []


def test_split_claims_keeps_abbreviations_and_numbers() -> None:
    claims = split_claims("Used streaming tools e.g. Kafka for ingestion at Acme [r1].")
    assert [c.labels for c in claims] == [("r1",)]
    claims = split_claims("Has 3.5 years of Node.js experience at Acme [r1].")
    assert [c.labels for c in claims] == [("r1",)]
    claims = split_claims("Worked with the U.S. team on payment systems [r1].")
    assert [c.labels for c in claims] == [("r1",)]


def test_split_claims_bullet_lines() -> None:
    claims = split_claims(
        "- Built Kafka pipelines at Acme [r1]\n* Led a team of five engineers [r2]\n"
        "3. Fits the role requirements well [j1]"
    )
    assert [c.labels for c in claims] == [("r1",), ("r2",), ("j1",)]
    assert claims[0].text.startswith("Built")


async def test_lexical_cited_claim_without_content_tokens_is_supported() -> None:
    result = await LexicalGroundedness(0.5).check(_ctx("It is as if it is so [r1]."))
    assert result.action == "allow"
