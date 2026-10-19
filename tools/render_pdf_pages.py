#!/usr/bin/env python3
"""Render PDF pages to PNG files for visual quality checks."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

try:
    import fitz
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing dependency: PyMuPDF. Install with `python -m pip install -r requirements.txt`."
    ) from exc


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    if not spec:
        return list(range(page_count))
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            pages.update(range(int(start_s) - 1, int(end_s)))
        else:
            pages.add(int(part) - 1)
    invalid = [p + 1 for p in pages if p < 0 or p >= page_count]
    if invalid:
        raise ValueError(f"Invalid page number(s): {invalid}")
    return sorted(pages)


def render_with_pdftoppm(pdf: Path, output_dir: Path, pages: list[int], dpi: int) -> bool:
    exe = shutil.which("pdftoppm")
    if not exe:
        return False
    for page_index in pages:
        prefix = output_dir / f"page-{page_index + 1:04d}"
        subprocess.run(
            [
                exe,
                "-png",
                "-r",
                str(dpi),
                "-f",
                str(page_index + 1),
                "-l",
                str(page_index + 1),
                str(pdf),
                str(prefix),
            ],
            check=True,
        )
    return True


def render_with_pymupdf(pdf: Path, output_dir: Path, pages: list[int], dpi: int) -> None:
    doc = fitz.open(pdf)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    for page_index in pages:
        pix = doc[page_index].get_pixmap(matrix=matrix, alpha=False)
        pix.save(output_dir / f"page-{page_index + 1:04d}.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("output_dir")
    parser.add_argument("--pages", help="1-based page ranges, for example: 1-3,8")
    parser.add_argument("--dpi", type=int, default=180)
    args = parser.parse_args()

    pdf = Path(args.pdf)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf)
    pages = parse_pages(args.pages, doc.page_count)
    doc.close()

    if not render_with_pdftoppm(pdf, output_dir, pages, args.dpi):
        render_with_pymupdf(pdf, output_dir, pages, args.dpi)
    print(f"Rendered {len(pages)} page(s) into {output_dir}")


if __name__ == "__main__":
    main()
