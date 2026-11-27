from uuid import uuid4

import pytest
import structlog

from app.core import pii
from app.core.pii import find_pii, redact_pii, set_pii_engine
from app.guardrails.base import GuardContext
from app.guardrails.pii import PiiGuard


def _raise(*_a: object, **_k: object) -> None:
    raise RuntimeError("boom")


@pytest.mark.usefixtures("presidio_engine")
def test_redacts_street_address() -> None:
    out = redact_pii("Lives at 42 Baker Street in town")
    assert "[ADDRESS]" in out
    assert "42 Baker" not in out


@pytest.mark.usefixtures("presidio_engine")
def test_redacts_city_location() -> None:
    out = redact_pii("Moved to Seattle last year")
    assert "Seattle" not in out
    assert "[ADDRESS]" in out


@pytest.mark.usefixtures("presidio_engine")
def test_existing_email_phone_rules_unchanged() -> None:
    assert redact_pii("mail jane.doe+x@example.co.uk now") == "mail [EMAIL] now"
    assert redact_pii("call +1 (415) 555-0132 today") == "call [PHONE] today"
    text = "2019-2023, Python 3.12, 10 years"
    assert redact_pii(text) == text


@pytest.mark.usefixtures("presidio_engine")
def test_skips_tokens_without_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii, "_analyzer", _raise)
    assert find_pii("chat_completed") == []


def test_presidio_failure_falls_back_to_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    set_pii_engine("presidio")
    monkeypatch.setattr(pii, "_analyzer", _raise)
    with structlog.testing.capture_logs() as logs:
        assert redact_pii("a@b.co in Seattle") == "[EMAIL] in Seattle"
        assert redact_pii("c@d.co in Seattle") == "[EMAIL] in Seattle"
    degraded = [e for e in logs if e["event"] == "pii_engine_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["error_type"] == "RuntimeError"


def test_regex_engine_never_loads_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii, "_analyzer", _raise)
    assert redact_pii("a@b.co in Seattle") == "[EMAIL] in Seattle"


def test_overlapping_spans_merge() -> None:
    merged = pii._merge(
        [pii.PiiSpan("ADDRESS", 5, 10), pii.PiiSpan("ADDRESS", 8, 14), pii.PiiSpan("EMAIL", 20, 25)]
    )
    assert merged == [pii.PiiSpan("ADDRESS", 5, 14), pii.PiiSpan("EMAIL", 20, 25)]


async def test_pii_guard_redacts_and_reports_kinds() -> None:
    ctx = GuardContext("chat_input", ("mail a@b.co",), "r1", uuid4())
    res = await PiiGuard().check(ctx)
    assert res.action == "redact"
    assert res.redacted == ("mail [EMAIL]",)
    assert res.reason == "pii:EMAIL"
    assert (res.guardrail, res.provider) == ("pii", "self")


async def test_pii_guard_reports_sorted_kinds_and_allows_clean_text() -> None:
    ctx = GuardContext("upload", ("a@b.co 415-555-0132", "clean"), "r1", None)
    res = await PiiGuard().check(ctx)
    assert res.reason == "pii:EMAIL,PHONE"
    assert res.redacted == ("[EMAIL] [PHONE]", "clean")
    clean = await PiiGuard().check(GuardContext("upload", ("clean",), "r1", None))
    assert clean.action == "allow" and clean.redacted is None


@pytest.mark.usefixtures("presidio_engine")
async def test_pii_guard_leaves_bare_locations_alone() -> None:
    q = "Is the role based in London or remote?"
    res = await PiiGuard().check(GuardContext("chat_input", (q,), "r1", uuid4()))
    assert res.action == "allow" and res.redacted is None


@pytest.mark.usefixtures("presidio_engine")
async def test_pii_guard_still_redacts_street_addresses() -> None:
    ctx = GuardContext("chat_input", ("Lives at 42 Baker Street",), "r1", uuid4())
    res = await PiiGuard().check(ctx)
    assert res.action == "redact"
    assert res.redacted == ("Lives at [ADDRESS]",)


@pytest.mark.usefixtures("presidio_engine")
def test_log_redaction_keeps_broad_locations() -> None:
    assert redact_pii("Moved to Seattle") == "Moved to [ADDRESS]"


def test_analyzer_is_built_once_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    built: list[int] = []

    def slow_build() -> object:
        built.append(1)
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(pii, "_analyzer_instance", None)
    monkeypatch.setattr(pii, "_build_analyzer", slow_build)
    threads = [threading.Thread(target=pii._analyzer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1


@pytest.mark.usefixtures("presidio_engine")
def test_logging_inside_presidio_does_not_recurse(monkeypatch: pytest.MonkeyPatch) -> None:
    class LoggingAnalyzer:
        def analyze(self, **_k: object) -> list[object]:
            redact_pii("spacy logged this text")  # what the log processor does
            return []

    monkeypatch.setattr(pii, "_analyzer", LoggingAnalyzer)
    assert redact_pii("plain words here") == "plain words here"


def test_warm_up_failure_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    set_pii_engine("presidio")
    monkeypatch.setattr(pii, "_analyzer", _raise)
    with structlog.testing.capture_logs() as logs:
        pii.warm_up()  # must not raise: a missing spaCy model cannot stop the app starting
        assert redact_pii("a@b.co in Seattle") == "[EMAIL] in Seattle"
    degraded = [e for e in logs if e["event"] == "pii_engine_degraded"]
    assert [e["error_type"] for e in degraded] == ["RuntimeError"]
