import pytest

from app.core.errors import AppError
from app.ingestion.pdf import parse_pdf, validate_jd_text, validate_pdf
from tests.fixtures.pdfs import make_pdf

MAX_BYTES = 5_242_880
MAX_PAGES = 10


@pytest.mark.parametrize(
    ("data_factory", "code"),
    [
        (lambda: b"hello", "not_pdf"),
        (lambda: make_pdf(["x"])[:200], "corrupt_pdf"),
        (lambda: make_pdf(["x"], encrypt=True), "encrypted_pdf"),
        (lambda: make_pdf(["p"] * 11), "too_many_pages"),
        (lambda: b"%PDF-" + b"0" * 5_242_880, "too_large"),
    ],
)
def test_validate_pdf_rejects(data_factory, code):
    with pytest.raises(AppError) as exc:
        validate_pdf(data_factory(), max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert exc.value.code == code
    assert exc.value.status == (413 if code == "too_large" else 400)


def test_parse_pdf_extracts_text_and_pages():
    data = make_pdf(["Alex Rivera\nSkills\nPython", "Experience\nAcme"])
    parsed = parse_pdf(data, max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert parsed.page_count == 2
    assert "Python" in parsed.text
    assert "Acme" in parsed.text


def test_parse_pdf_scanned():
    with pytest.raises(AppError) as exc:
        parse_pdf(make_pdf(["x"], image_only=True), max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert exc.value.code == "scanned_pdf"


def test_sha256_stable_across_whitespace():
    a = parse_pdf(make_pdf(["Alex Rivera\nPython"]), max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    b = parse_pdf(make_pdf(["Alex Rivera   \nPython  "]), max_bytes=MAX_BYTES, max_pages=MAX_PAGES)
    assert a.sha256 == b.sha256


def test_validate_jd_text_bounds():
    with pytest.raises(AppError) as short:
        validate_jd_text("a" * 49)
    assert short.value.code == "jd_too_short"
    with pytest.raises(AppError) as long:
        validate_jd_text("a" * 20_001)
    assert long.value.code == "jd_too_long"
    assert validate_jd_text("  " + "ok" * 30 + "  ") == "ok" * 30
