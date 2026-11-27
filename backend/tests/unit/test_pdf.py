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


def test_jd_text_accepts_exact_bounds():
    assert len(validate_jd_text("a" * 50)) == 50
    assert len(validate_jd_text("a" * 20_000)) == 20_000


def test_validate_pdf_accepts_exact_limits():
    data = make_pdf(["p"] * 10)
    validate_pdf(data, max_bytes=len(data), max_pages=10)
    with pytest.raises(AppError) as exc:
        validate_pdf(data, max_bytes=len(data) - 1, max_pages=10)
    assert exc.value.code == "too_large"


def test_normalization_crlf_nfc_and_blank_runs():
    out = validate_jd_text("line one  \r\né two\n\n\n\n\nthree " + "x" * 50)
    assert "\r" not in out
    assert "é two" in out
    assert "é" not in out
    assert "two\n\n\nthree" in out
    assert "\n\n\n\n" not in out
    assert out.startswith("line one\n")
