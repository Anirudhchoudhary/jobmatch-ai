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
