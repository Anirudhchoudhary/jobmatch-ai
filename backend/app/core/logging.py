"""Structured logging with PII redaction.

All log output passes through ``redact_pii`` before rendering. Phase 4 extends
``redact_pii`` (addresses via Presidio) under the same name.
"""

import logging
import re
import sys
from typing import Any
from urllib.parse import unquote

import structlog
from structlog.typing import EventDict, Processor

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Optional +country, digits separated by whitespace, parentheses, dots or dashes.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().-]{7,}\d(?!\w)")
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15
_HANDLER_MARK = "_jobmatch_handler"


def _phone_sub(match: re.Match[str]) -> str:
    digits = sum(c.isdigit() for c in match.group(0))
    if _MIN_PHONE_DIGITS <= digits <= _MAX_PHONE_DIGITS:
        return "[PHONE]"
    return match.group(0)


def redact_pii(text: str) -> str:
    """Replace emails and phone numbers in ``text`` with placeholders.

    Percent-encoded text (e.g. URL query strings) is URL-decoded first so encoded
    PII cannot bypass redaction; the returned text is then the decoded form.
    """
    if "%" in text:
        text = unquote(text)
    return _PHONE_RE.sub(_phone_sub, _EMAIL_RE.sub("[EMAIL]", text))


def redact_value(value: object) -> object:
    """Recursively redact strings (and string forms of other objects)."""
    if isinstance(value, str):
        return redact_pii(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {redact_value(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    if isinstance(value, set | frozenset):
        return [redact_value(v) for v in value]
    return redact_pii(str(value))


def _redact_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    return {k: redact_value(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", *, json: bool = True) -> None:
    """Route structlog and stdlib logging to stdout as redacted JSON. Idempotent."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _redact_processor,  # last step before rendering
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    # uvicorn installs its own non-propagating handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    root.setLevel(level.upper())
