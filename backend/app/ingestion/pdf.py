"""PDF validation/parsing and JD text validation. Never logs document text."""

import hashlib
import logging
import re
import unicodedata
import warnings
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

from app.core.errors import AppError

_BLANK_RUNS = re.compile(r"\n{4,}")


@dataclass(frozen=True)
class ParsedPdf:
    text: str
    page_count: int
    sha256: str


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n\n", text).strip()


def _open(data: bytes, *, max_bytes: int, max_pages: int) -> PdfReader:
    if len(data) > max_bytes:
        raise AppError("too_large", "That file is too large. The limit is 5 MB.", 413)
    if not data.startswith(b"%PDF-"):
        raise AppError("not_pdf", "Please upload a PDF file.")
    pypdf_logger = logging.getLogger("pypdf")
    previous_disabled = pypdf_logger.disabled
    pypdf_logger.disabled = True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise AppError("encrypted_pdf", "Password-protected PDFs aren't supported.")
            page_count = len(reader.pages)
    except AppError:
        raise
    except Exception as exc:
        raise AppError("corrupt_pdf", "We couldn't read that PDF. It may be corrupt.") from exc
    finally:
        pypdf_logger.disabled = previous_disabled
    if page_count > max_pages:
        raise AppError("too_many_pages", f"That PDF has too many pages. The limit is {max_pages}.")
    return reader


def validate_pdf(data: bytes, *, max_bytes: int, max_pages: int) -> None:
    _open(data, max_bytes=max_bytes, max_pages=max_pages)


def parse_pdf(data: bytes, *, max_bytes: int, max_pages: int) -> ParsedPdf:
    reader = _open(data, max_bytes=max_bytes, max_pages=max_pages)
    pypdf_logger = logging.getLogger("pypdf")
    previous_disabled = pypdf_logger.disabled
    pypdf_logger.disabled = True
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise AppError("corrupt_pdf", "We couldn't read that PDF. It may be corrupt.") from exc
    finally:
        pypdf_logger.disabled = previous_disabled
    text = _normalize("\n\n".join(pages))
    if not text:
        raise AppError(
            "scanned_pdf", "This PDF has no selectable text. Scanned PDFs aren't supported."
        )
    return ParsedPdf(
        text=text,
        page_count=len(pages),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def validate_jd_text(text: str, *, min_chars: int = 50, max_chars: int = 20_000) -> str:
    normalized = _normalize(text)
    if len(normalized) < min_chars:
        raise AppError(
            "jd_too_short", f"The job description is too short. Minimum is {min_chars} characters."
        )
    if len(normalized) > max_chars:
        raise AppError(
            "jd_too_long", f"The job description is too long. The limit is {max_chars} characters."
        )
    return normalized
