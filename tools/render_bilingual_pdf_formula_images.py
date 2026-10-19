#!/usr/bin/env python3
"""Render bilingual MinerU content while preserving equations as PDF crops."""

from __future__ import annotations

import argparse
import html
import io
import json
import re
from pathlib import Path
from typing import Any

import fitz
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


BILINGUAL_SEPARATOR = "\n\n译文："


def normalize_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = text.replace("\ufffd", "")
    text = text.replace("☉", "")
    text = text.replace("•", "·")
    return text.strip()


def paragraph_text(text: str) -> str:
    escaped = html.escape(normalize_text(text))
    escaped = re.sub(
        r"(https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)",
        r"<font color='blue'>\1</font>",
        escaped,
    )
    return escaped.replace("\n", "<br/>")


def register_fonts() -> str:
    font_name = "STSong-Light"
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    except Exception:
        pass
    return font_name


def split_bilingual(text: str) -> tuple[str, str | None]:
    if BILINGUAL_SEPARATOR in text:
        source, translation = text.split(BILINGUAL_SEPARATOR, 1)
        return source.strip(), translation.strip()
    if "\n译文：" in text:
        source, translation = text.split("\n译文：", 1)
        return source.strip(), translation.strip()
    return text.strip(), None


def add_bilingual_paragraph(story: list[Any], text: str, styles: dict[str, ParagraphStyle]) -> None:
    source, translation = split_bilingual(text)
    if source:
        story.append(Paragraph(paragraph_text(source), styles["source"]))
    if translation:
        story.append(Paragraph("译文：" + paragraph_text(translation), styles["translation"]))
    story.append(Spacer(1, 2.5 * mm))


def crop_equation_image(
    pdf_doc: fitz.Document,
    page_idx: int,
    bbox: list[float],
    coord_size: tuple[float, float],
    zoom: float = 3.0,
    padding: float = 3.0,
) -> tuple[io.BytesIO, float, float]:
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
    rect.x0 = max(0, rect.x0 - padding)
    rect.y0 = max(0, rect.y0 - padding)
    rect.x1 = min(page.rect.width, rect.x1 + padding)
    rect.y1 = min(page.rect.height, rect.y1 + padding)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    data = io.BytesIO(pix.tobytes("png"))
    data.seek(0)
    return data, rect.width, rect.height


def add_equation_crop(
    story: list[Any],
    pdf_doc: fitz.Document,
    item: dict[str, Any],
    coord_sizes: dict[int, tuple[float, float]],
    max_width: float,
) -> None:
    bbox = item.get("bbox")
    page_idx = item.get("page_idx")
    if not isinstance(bbox, list) or page_idx is None:
        return
    try:
        image_data, width_pt, height_pt = crop_equation_image(
            pdf_doc,
            int(page_idx),
            bbox,
            coord_sizes.get(int(page_idx), (928.0, 943.0)),
        )
    except Exception:
        return
    draw_width = min(max_width, max(35 * mm, width_pt))
    draw_height = max(8 * mm, draw_width * height_pt / max(width_pt, 1))
    max_height = 95 * mm
    if draw_height > max_height:
        scale = max_height / draw_height
        draw_width *= scale
        draw_height = max_height
    img = Image(image_data, width=draw_width, height=draw_height)
    img.hAlign = "CENTER"
    story.append(img)
    story.append(Spacer(1, 3 * mm))


def build_styles(font_name: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "source": ParagraphStyle(
            "source",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#555555"),
            alignment=TA_LEFT,
            spaceAfter=1.5 * mm,
        ),
        "translation": ParagraphStyle(
            "translation",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=10.5,
            leading=15,
            textColor=colors.black,
            alignment=TA_LEFT,
            spaceAfter=2 * mm,
        ),
        "heading": ParagraphStyle(
            "heading",
            parent=base["Heading1"],
            fontName=font_name,
            fontSize=16,
            leading=20,
            textColor=colors.black,
            alignment=TA_LEFT,
            spaceBefore=4 * mm,
            spaceAfter=3 * mm,
        ),
        "small_center": ParagraphStyle(
            "small_center",
            parent=base["BodyText"],
            fontName=font_name,
            fontSize=9,
            leading=11,
            alignment=TA_CENTER,
        ),
    }


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
        if max_x < 800:
            max_x = 928.0
        if max_y < 800:
            max_y = 943.0
        sizes[page_idx] = (max_x, max_y)
    return sizes


def content_to_story(content: list[dict[str, Any]], source_pdf: Path, max_width: float) -> list[Any]:
    pdf_doc = fitz.open(source_pdf)
    coord_sizes = infer_coord_sizes(content, pdf_doc)
    font_name = register_fonts()
    styles = build_styles(font_name)
    story: list[Any] = []

    for item in content:
        item_type = item.get("type")
        if item_type == "equation":
            add_equation_crop(story, pdf_doc, item, coord_sizes, max_width)
            continue

        if item_type == "text":
            text = item.get("text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            level = int(item.get("text_level") or 0)
            if level > 0:
                source, translation = split_bilingual(text)
                if source:
                    story.append(Paragraph(paragraph_text(source), styles["heading"]))
                if translation:
                    story.append(Paragraph("译文：" + paragraph_text(translation), styles["translation"]))
                story.append(Spacer(1, 2 * mm))
            else:
                add_bilingual_paragraph(story, text, styles)
            continue

        if item_type == "list":
            parts: list[str] = []
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            if isinstance(item.get("list_body"), str):
                parts.append(item["list_body"])
            for entry in item.get("list_items", []):
                if isinstance(entry, str):
                    parts.append("- " + entry)
            for part in parts:
                add_bilingual_paragraph(story, part, styles)
            continue

        if item_type == "table":
            for caption in item.get("table_caption", []):
                if isinstance(caption, str):
                    add_bilingual_paragraph(story, caption, styles)
            body = item.get("table_body") or item.get("text")
            if isinstance(body, str):
                add_bilingual_paragraph(story, body, styles)
            continue

        text = item.get("text")
        if isinstance(text, str) and text.strip():
            add_bilingual_paragraph(story, text, styles)

    return story


def render(content_list: Path, source_pdf: Path, out_pdf: Path) -> None:
    content = json.loads(content_list.read_text(encoding="utf-8"))
    if not isinstance(content, list):
        raise SystemExit("content list must be a JSON array")
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_pdf),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
    )
    story = content_to_story(content, source_pdf, doc.width)
    doc.build(story)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content_list")
    parser.add_argument("source_pdf")
    parser.add_argument("out_pdf")
    args = parser.parse_args()
    render(Path(args.content_list), Path(args.source_pdf), Path(args.out_pdf))


if __name__ == "__main__":
    main()
