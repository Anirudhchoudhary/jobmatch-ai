"""Single PII engine shared by logging and the PII guard.

Emails and phones use regexes (always on). With the ``presidio`` engine, Presidio
(spaCy ``en_core_web_sm`` plus a street-address pattern) also finds addresses.
This module must not import settings; ``configure_logging`` selects the engine.
"""

import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

import structlog

if TYPE_CHECKING:
    from presidio_analyzer import AnalyzerEngine

PiiKind = Literal["EMAIL", "PHONE", "ADDRESS"]
PiiEngine = Literal["regex", "presidio"]
# "broad" also redacts bare locations (cities); "street" only street addresses.
PiiScope = Literal["broad", "street"]
_ENTITIES: dict[PiiScope, list[str]] = {
    "broad": ["LOCATION", "STREET_ADDRESS"],
    "street": ["STREET_ADDRESS"],
}

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
_analyzer_lock = threading.Lock()
_analyzer_instance: "AnalyzerEngine | None" = None
_reentry = threading.local()


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


def _analyzer() -> "AnalyzerEngine":
    """The shared analyzer, built once even when threads race on the first call."""
    global _analyzer_instance
    with _analyzer_lock:
        if _analyzer_instance is None:
            with _regex_only_here():
                _analyzer_instance = _build_analyzer()
        return _analyzer_instance


@contextmanager
def _regex_only_here() -> Iterator[None]:
    """Presidio and spaCy log while loading and analysing, and our log processor redacts what
    is logged. Nested redaction on this thread therefore uses regexes only, which avoids both
    unbounded recursion and waiting on the lock this thread holds."""
    _reentry.active = True
    try:
        yield
    finally:
        _reentry.active = False


def warm_up() -> None:
    """Build the Presidio analyzer now (a no-op for the regex engine)."""
    if _engine == "presidio":
        _analyzer()


def _build_analyzer() -> "AnalyzerEngine":
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


def _presidio_spans(text: str, scope: PiiScope) -> list[PiiSpan]:
    analyzer = _analyzer()
    with _regex_only_here():
        results = analyzer.analyze(text=text, language="en", entities=_ENTITIES[scope])
    return [PiiSpan("ADDRESS", r.start, r.end) for r in results]


def _wants_presidio(text: str) -> bool:
    return (
        _engine == "presidio"
        and not _degraded
        and not getattr(_reentry, "active", False)
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


def find_pii(text: str, scope: PiiScope = "broad") -> list[PiiSpan]:
    """Return merged, non-overlapping PII spans in ``text`` (``scope`` as in ``PiiScope``)."""
    global _degraded
    spans = _regex_spans(text)
    if _wants_presidio(text):
        try:
            spans += _presidio_spans(text, scope)
        except Exception as exc:  # logging must never crash
            _degraded = True
            _log.error("pii_engine_degraded", error_type=type(exc).__name__)
    return _merge(spans)


def redact_pii(text: str, scope: PiiScope = "broad") -> str:
    """Replace emails, phones and addresses in ``text`` with placeholders.

    Percent-encoded text (e.g. URL query strings) is URL-decoded first so encoded
    PII cannot bypass redaction; the returned text is then the decoded form.
    """
    if "%" in text:
        text = unquote(text)
    out: list[str] = []
    pos = 0
    for span in find_pii(text, scope):
        out.append(text[pos : span.start])
        out.append(f"[{span.kind}]")
        pos = span.end
    out.append(text[pos:])
    return "".join(out)
