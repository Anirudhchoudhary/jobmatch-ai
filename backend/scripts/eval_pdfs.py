"""Render every eval case's resume to evals/dataset/pdfs/<id>.pdf.

`python -m scripts.eval_pdfs` writes them; `--check` writes nothing and exits 1 listing ids
whose committed PDF is missing or differs from a fresh render.
"""

import argparse
import sys

from evals.dataset import DATASET_DIR, load_cases, render_resume_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed PDFs, write nothing")
    args = parser.parse_args(argv)

    pdf_dir = DATASET_DIR / "pdfs"
    stale: list[str] = []
    for case in load_cases():
        path = pdf_dir / f"{case.id}.pdf"
        data = render_resume_pdf(case.resume)
        if args.check:
            if not path.exists() or path.read_bytes() != data:
                stale.append(case.id)
        else:
            pdf_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path}")
    if stale:
        print(f"missing or stale PDFs: {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
