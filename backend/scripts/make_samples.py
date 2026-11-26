"""Render app/samples/resume.md to resume.pdf (dev script; fpdf2 is a dev dependency).

Deterministic: fixed creation date and metadata. Text must stay latin-1 (core fonts).
"""

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

SAMPLES = Path(__file__).resolve().parents[1] / "app" / "samples"
MAX_PAGES = 2


def render(markdown: str) -> bytes:
    pdf = FPDF(format="Letter")
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))
    pdf.set_title("Alex Rivera - Resume (sample)")
    pdf.set_author("JobMatch AI samples")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    def line(text: str, style: str, size: int, gap: float) -> None:
        pdf.set_font("Helvetica", style=style, size=size)
        pdf.multi_cell(0, gap, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    for raw in markdown.splitlines():
        if raw.startswith("# "):
            line(raw[2:], "B", 18, 8)
        elif raw.startswith("## "):
            pdf.ln(2)
            line(raw[3:], "B", 12, 6)
        elif raw.strip():
            line(raw, "", 10, 4.8)
        else:
            pdf.ln(1.5)
    if pdf.page > MAX_PAGES:
        raise SystemExit(f"resume renders to {pdf.page} pages; max is {MAX_PAGES}")
    return bytes(pdf.output())


def main() -> None:
    md = (SAMPLES / "resume.md").read_text(encoding="utf-8")
    (SAMPLES / "resume.pdf").write_bytes(render(md))
    print(f"wrote {SAMPLES / 'resume.pdf'}")


if __name__ == "__main__":
    main()
