"""Build small PDFs for tests."""

from fpdf import FPDF


def make_pdf(pages: list[str], *, encrypt: bool = False, image_only: bool = False) -> bytes:
    pdf = FPDF()
    if encrypt:
        pdf.set_encryption(owner_password="owner", user_password="user")
    for text in pages:
        pdf.add_page()
        if image_only:
            pdf.rect(20, 20, 50, 50, style="F")
        else:
            pdf.set_font("Helvetica", size=12)
            pdf.multi_cell(0, 8, text)
    return bytes(pdf.output())
