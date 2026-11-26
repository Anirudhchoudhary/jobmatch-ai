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
