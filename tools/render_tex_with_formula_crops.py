#!/usr/bin/env python3
"""Render bilingual content with inline math as TeX and block equations as PDF crops."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import fitz
from PIL import Image as PILImage

from render_translated_markdown_tex import line_to_latex


BILINGUAL_SEPARATOR = "\n\n译文："


def split_bilingual(text: str) -> tuple[str, str | None]:
    if BILINGUAL_SEPARATOR in text:
        source, translation = text.split(BILINGUAL_SEPARATOR, 1)
        return source.strip(), translation.strip()
    if "\n译文：" in text:
        source, translation = text.split("\n译文：", 1)
        return source.strip(), translation.strip()
    return text.strip(), None


def infer_coord_sizes(content: list[dict[str, Any]], pdf_doc: fitz.Document) -> dict[int, tuple[float, float]]:
    sizes: dict[int, tuple[float, float]] = {}
    for page_idx in range(pdf_doc.page_count):
        boxes = [
            item.get("bbox")
            for item in content
            if item.get("page_idx") == page_idx and isinstance(item.get("bbox"), list)
        ]
        if not boxes:
            continue
        max_x = max(float(box[2]) for box in boxes)
        max_y = max(float(box[3]) for box in boxes)
        sizes[page_idx] = (max(max_x, 928.0), max(max_y, 943.0))
    return sizes


def crop_equation(
    pdf_doc: fitz.Document,
    page_idx: int,
    bbox: list[float],
    coord_size: tuple[float, float],
    out_path: Path,
) -> tuple[float, float]:
    page = pdf_doc[page_idx]
    coord_width, coord_height = coord_size
    scale_x = page.rect.width / coord_width
    scale_y = page.rect.height / coord_height
    rect = fitz.Rect(
        bbox[0] * scale_x,
        bbox[1] * scale_y,
        bbox[2] * scale_x,
        bbox[3] * scale_y,
    )
    pad_x = 6
    pad_top = 28
    pad_bottom = 10
    rect.x0 = max(0, rect.x0 - pad_x)
    rect.y0 = max(0, rect.y0 - pad_top)
    rect.x1 = min(page.rect.width, rect.x1 + pad_x)
    rect.y1 = min(page.rect.height, rect.y1 + pad_bottom)
    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=rect, alpha=False)
    pix.save(out_path)
    return rect.width, rect.height


def add_bilingual(body: list[str], text: str) -> None:
    source, translation = split_bilingual(text)
    if source:
        body.append(r"\begin{sourceblock}")
        for line in source.splitlines():
            if line.strip():
                body.append(line_to_latex(line.strip()))
                body.append("")
        body.append(r"\end{sourceblock}")
    if translation:
        body.append(r"\begin{translationblock}")
        body.append("译文：" + line_to_latex(translation))
        body.append(r"\end{translationblock}")
    body.append("")


def add_image_asset(
    body: list[str],
    img_source: Path,
    eq_dir: Path,
    image_index: int,
    max_width: float = 430.0,
) -> None:
    out_img_path = eq_dir / f"asset-{image_index:04d}{img_source.suffix.lower() or '.png'}"
    shutil.copy2(img_source, out_img_path)
    with PILImage.open(out_img_path) as im:
        width_px, height_px = im.size
    draw_width = min(max_width, max(90.0, width_px / 3.0))
    draw_height = draw_width * height_px / max(width_px, 1)
    if draw_height > 430:
        draw_width *= 430 / draw_height
    tex_img_path = Path("equation_crops") / out_img_path.name
    body.append(r"\begin{center}")
    body.append(rf"\includegraphics[width={draw_width:.2f}pt]{{{tex_img_path.as_posix()}}}")
    body.append(r"\end{center}")
    body.append("")


def caption_values(item: dict[str, Any], item_type: str) -> list[str]:
    fields = [
        f"{item_type}_caption",
        f"{item_type}_footnote",
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
    ]
    values: list[str] = []
    for field in fields:
        value = item.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(entry for entry in value if isinstance(entry, str))
    return values


def content_to_tex_body(
    content: list[dict[str, Any]],
    source_pdf: Path,
    work_dir: Path,
    asset_base: Path | None,
) -> str:
    pdf_doc = fitz.open(source_pdf)
    coord_sizes = infer_coord_sizes(content, pdf_doc)
    eq_dir = work_dir / "equation_crops"
    eq_dir.mkdir(parents=True, exist_ok=True)
    body: list[str] = []
    eq_index = 0
    image_index = 0

    for item in content:
        item_type = item.get("type")
        if item_type in {"equation", "table", "image", "chart"}:
            if item_type != "equation":
                for caption in caption_values(item, item_type):
                    add_bilingual(body, caption)
            bbox = item.get("bbox")
            page_idx = item.get("page_idx")
            img_source: Path | None = None
            img_path = item.get("img_path")
            if asset_base and isinstance(img_path, str):
                candidate = asset_base / img_path
                if candidate.exists():
                    img_source = candidate
            if item_type != "equation" and img_source:
                image_index += 1
                add_image_asset(body, img_source, eq_dir, image_index)
                body_text = item.get("text")
                if isinstance(body_text, str) and body_text.strip() and "<table" not in body_text:
                    add_bilingual(body, body_text)
                continue
            if item_type != "equation":
                body_text = item.get("table_body") or item.get("text")
                if isinstance(body_text, str) and body_text.strip() and "<table" not in body_text:
                    add_bilingual(body, body_text)
                continue
            if img_source or (isinstance(bbox, list) and page_idx is not None):
                eq_index += 1
                out_img_path = eq_dir / f"eq-{eq_index:04d}.png"
                try:
                    tag_match = re.search(r"\\tag\{([^}]+)\}", str(item.get("text", "")))
                    tag = tag_match.group(1) if tag_match else None
                    if img_source:
                        with PILImage.open(img_source) as im:
                            if tag and im.width > 120:
                                im = im.crop((0, 0, im.width - min(28, im.width // 12), im.height))
                            im.save(out_img_path)
                        with PILImage.open(out_img_path) as im:
                            width_px, height_px = im.size
                        draw_width = min(430.0, max(60.0, width_px / 3.0))
                        draw_height = draw_width * height_px / max(width_px, 1)
                    else:
                        width_pt, height_pt = crop_equation(
                            pdf_doc,
                            int(page_idx),
                            bbox,
                            coord_sizes.get(int(page_idx), (928.0, 943.0)),
                            out_img_path,
                        )
                        draw_width = min(430.0, max(60.0, width_pt * 1.05))
                        draw_height = draw_width * height_pt / max(width_pt, 1)
                    if draw_height > 250:
                        draw_width *= 250 / draw_height
                    body.append(r"\begin{center}")
                    tex_img_path = Path("equation_crops") / out_img_path.name
                    if tag:
                        body.append(
                            rf"\includegraphics[width={draw_width:.2f}pt]{{{tex_img_path.as_posix()}}}"
                            rf"\quad ({tag})"
                        )
                    else:
                        body.append(
                            rf"\includegraphics[width={draw_width:.2f}pt]{{{tex_img_path.as_posix()}}}"
                        )
                    body.append(r"\end{center}")
                    body.append("")
                except Exception:
                    pass
            continue

        if item_type == "text":
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            level = int(item.get("text_level") or 0)
            if level > 0:
                source, translation = split_bilingual(text)
                if source:
                    command = "section" if level == 1 else "subsection"
                    body.append(rf"\{command}*{{{line_to_latex(source)}}}")
                if translation:
                    body.append(r"\begin{translationblock}")
                    body.append("译文：" + line_to_latex(translation))
                    body.append(r"\end{translationblock}")
                body.append("")
            else:
                add_bilingual(body, text)
            continue

        if item_type == "list":
            for field in ("text", "list_body"):
                if isinstance(item.get(field), str):
                    add_bilingual(body, item[field])
            for entry in item.get("list_items", []):
                if isinstance(entry, str):
                    add_bilingual(body, "- " + entry)
            continue

        text = item.get("text")
        if isinstance(text, str) and text.strip():
            add_bilingual(body, text)

    return "\n".join(body)


def build_document(body: str) -> str:
    return rf"""
\documentclass[11pt]{{article}}
\usepackage[a4paper,margin=22mm]{{geometry}}
\usepackage{{fontspec}}
\usepackage{{xeCJK}}
\usepackage{{amsmath,amssymb,mathtools}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{microtype}}
\usepackage{{hyperref}}
\setmainfont{{Times New Roman}}
\setsansfont{{Times New Roman}}
\setmonofont{{Menlo}}
\setCJKmainfont{{Songti SC}}
\setCJKsansfont{{Songti SC}}
\setCJKmonofont{{Songti SC}}
\hypersetup{{colorlinks=true,linkcolor=blue,urlcolor=blue}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{5pt}}
\newenvironment{{sourceblock}}{{\begingroup\small\color{{gray!75}}}}{{\par\endgroup}}
\newenvironment{{translationblock}}{{\begingroup\normalsize\color{{black}}\bfseries}}{{\par\endgroup}}
\begin{{document}}
{body}
\end{{document}}
"""


def render(
    content_list: Path,
    source_pdf: Path,
    out_pdf: Path,
    work_dir: Path,
    asset_base: Path | None,
) -> None:
    content = json.loads(content_list.read_text(encoding="utf-8"))
    if not isinstance(content, list):
        raise SystemExit("content list must be a JSON array")
    work_dir.mkdir(parents=True, exist_ok=True)
    tex_path = work_dir / "translated_formula_crops.tex"
    tex_path.write_text(
        build_document(content_to_tex_body(content, source_pdf, work_dir, asset_base)),
        encoding="utf-8",
    )
    xelatex = shutil.which("xelatex") or "/Library/TeX/texbin/xelatex"
    subprocess.run(
        [xelatex, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
        cwd=work_dir,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    generated = work_dir / "translated_formula_crops.pdf"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(generated, out_pdf)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content_list")
    parser.add_argument("source_pdf")
    parser.add_argument("out_pdf")
    parser.add_argument("--work-dir", default="tmp/formula-crop-tex")
    parser.add_argument("--asset-base", help="MinerU txt directory containing images/")
    args = parser.parse_args()
    render(
        Path(args.content_list),
        Path(args.source_pdf),
        Path(args.out_pdf),
        Path(args.work_dir),
        Path(args.asset_base) if args.asset_base else None,
    )


if __name__ == "__main__":
    main()
