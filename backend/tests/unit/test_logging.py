import json
import logging

import pytest
import structlog

from app.core.logging import configure_logging, redact_pii, redact_value


def test_redacts_email() -> None:
    assert redact_pii("mail jane.doe+x@example.co.uk now") == "mail [EMAIL] now"


@pytest.mark.parametrize(
    "phone", ["+1 (415) 555-0132", "415-555-0132", "+91 98765 43210", "07911 123456"]
)
def test_redacts_phones(phone: str) -> None:
    assert redact_pii(f"call {phone} today") == "call [PHONE] today"


def test_does_not_redact_years_or_versions() -> None:
    text = "2019-2023, Python 3.12, 10 years"
    assert redact_pii(text) == text


def test_redact_value_nested() -> None:
    value = {"u": {"emails": ["a@b.io"]}, "n": 3}
    assert redact_value(value) == {"u": {"emails": ["[EMAIL]"]}, "n": 3}


def test_log_output_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    structlog.reset_defaults()
    configure_logging()
    configure_logging()  # idempotent
    logger = structlog.get_logger("test")
    logger.info("signup", email="a@b.io")
    try:
        raise ValueError("bad a@b.io")
    except ValueError:
        logger.exception("failed")
    out = capsys.readouterr().out
    assert "a@b.io" not in out
    assert "[EMAIL]" in out
    assert out.count("signup") == 1


def test_redact_value_non_string_leaves_and_keys() -> None:
    assert redact_value({"a@b.io": 1}) == {"[EMAIL]": 1}
    assert redact_value(ValueError("a@b.io")) == "a@b.io".replace("a@b.io", "[EMAIL]")
    assert redact_value({"x@y.io"}) == ["[EMAIL]"]
    assert redact_value(None) is None


def test_exception_object_field_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.get_logger("t").info("m", err=ValueError("a@b.io"))
    out = capsys.readouterr().out
    assert "a@b.io" not in out
    assert "[EMAIL]" in out


def test_uvicorn_loggers_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    lg = logging.getLogger("uvicorn.access")
    lg.addHandler(logging.StreamHandler())
    lg.propagate = False
    configure_logging()
    lg.info("GET /x?email=%s", "a@b.io")
    out = capsys.readouterr().out
    assert "a@b.io" not in out
    assert "[EMAIL]" in out


def test_redacts_percent_encoded_email() -> None:
    out = redact_pii("GET /health?email=jane%40example.com")
    assert "example.com" not in out
    assert "[EMAIL]" in out


def test_redacts_percent_encoded_phone() -> None:
    out = redact_pii("GET /x?phone=%2B1%20415%20555%200132")
    assert "415" not in out
    assert "[PHONE]" in out


def test_percent_without_pii_is_unchanged() -> None:
    assert redact_pii("progress 50% done") == "progress 50% done"


def test_uvicorn_access_percent_encoded_pii_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5000",
        "GET",
        "/health?email=jane%40example.com&phone=%2B1%20415%20555%200132",
        "1.1",
        200,
    )
    out = capsys.readouterr().out
    event = str(json.loads(out)["event"])
    assert "jane" not in event
    assert "example.com" not in event
    assert "415" not in event
    assert "[EMAIL]" in event
    assert "[PHONE]" in event


@pytest.mark.usefixtures("presidio_engine")
def test_log_line_redacts_address(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(pii_engine="presidio")
    structlog.get_logger("t").info("note_saved", note="42 Baker Street")
    out = capsys.readouterr().out
    assert "Baker" not in out
    assert "[ADDRESS]" in out
