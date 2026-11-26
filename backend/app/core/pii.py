"""Single PII engine shared by logging and the PII guard.

Emails and phones use regexes (always on). With the ``presidio`` engine, Presidio
(spaCy ``en_core_web_sm`` plus a street-address pattern) also finds addresses.
This module must not import settings; ``configure_logging`` selects the engine.
"""

import re
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

import structlog

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine

PiiKind = Literal["EMAIL", "PHONE", "ADDRESS"]
PiiEngine = Literal["regex", "presidio"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Optional +country, digits separated by whitespace, parentheses, dots or dashes.
_PHONE_RE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().-]{7,}\d(?!\w)")
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15
_STREET_RE = (
    r"\b\d{1,6}\s+(?:[A-Z][a-z]+\s){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl)\b\.?"
)
_STREET_SCORE = 0.6
_SPACY_MODEL = "en_core_web_sm"

_log = structlog.get_logger(__name__)
_engine: PiiEngine = "regex"
_degraded = False


@dataclass(frozen=True)
class PiiSpan:
    kind: PiiKind
    start: int
    end: int


def set_pii_engine(engine: PiiEngine) -> None:
    """Select the engine for this process (clears any earlier degradation)."""
    global _engine, _degraded
    _engine = engine
    _degraded = False


@cache
def _analyzer() -> "AnalyzerEngine":
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
        }
    ).create_engine()
    registry = RecognizerRegistry(supported_languages=["en"])
    registry.load_predefined_recognizers(nlp_engine=nlp_engine, languages=["en"])
    registry.add_recognizer(
        PatternRecognizer(
            supported_entity="STREET_ADDRESS",
            patterns=[Pattern("street_address", _STREET_RE, _STREET_SCORE)],
            supported_language="en",
        )
    )
    return AnalyzerEngine(nlp_engine=nlp_engine, registry=registry, supported_languages=["en"])


def _regex_spans(text: str) -> list[PiiSpan]:
    spans = [PiiSpan("EMAIL", m.start(), m.end()) for m in _EMAIL_RE.finditer(text)]
    for m in _PHONE_RE.finditer(text):
        digits = sum(c.isdigit() for c in m.group(0))
        if _MIN_PHONE_DIGITS <= digits <= _MAX_PHONE_DIGITS:
            spans.append(PiiSpan("PHONE", m.start(), m.end()))
    return spans


def _presidio_spans(text: str) -> list[PiiSpan]:
    results = _analyzer().analyze(text=text, language="en", entities=["LOCATION", "STREET_ADDRESS"])
    return [PiiSpan("ADDRESS", r.start, r.end) for r in results]


def _wants_presidio(text: str) -> bool:
    return (
        _engine == "presidio"
        and not _degraded
        and any(c.isspace() for c in text)
        and any(c.isalpha() for c in text)
    )


def _merge(spans: list[PiiSpan]) -> list[PiiSpan]:
    merged: list[PiiSpan] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            if span.end > last.end:
                merged[-1] = PiiSpan(last.kind, last.start, span.end)
        else:
            merged.append(span)
    return merged


def find_pii(text: str) -> list[PiiSpan]:
    """Return merged, non-overlapping PII spans in ``text``."""
    global _degraded
    spans = _regex_spans(text)
    if _wants_presidio(text):
        try:
            spans += _presidio_spans(text)
        except Exception as exc:  # logging must never crash
            _degraded = True
            _log.error("pii_engine_degraded", error_type=type(exc).__name__)
    return _merge(spans)


def redact_pii(text: str) -> str:
    """Replace emails, phones and addresses in ``text`` with placeholders.

    Percent-encoded text (e.g. URL query strings) is URL-decoded first so encoded
    PII cannot bypass redaction; the returned text is then the decoded form.
    """
    if "%" in text:
        text = unquote(text)
    out: list[str] = []
    pos = 0
    for span in find_pii(text):
        out.append(text[pos : span.start])
        out.append(f"[{span.kind}]")
        pos = span.end
    out.append(text[pos:])
    return "".join(out)
