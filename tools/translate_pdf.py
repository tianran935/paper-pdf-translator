#!/usr/bin/env python3
"""Translate English prose in a PDF while preserving layout-heavy content."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import fitz
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing dependency: PyMuPDF. Install with `python -m pip install -r requirements.txt`."
    ) from exc

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing dependency: openai. Install with `python -m pip install -r requirements.txt`."
    ) from exc


MATH_CHARS = set("=+-*/^_<>≤≥≈≠±×÷∑∏∫√∞∂∇∈∉⊂⊆⊕⊗→←↔⇒⇔αβγδεζηθικλμνξοπρστυφχψω")

PROTECTED_RE = re.compile(
    r"""
    (https?://\S+)
    |(\b[\w.-]+@[\w.-]+\.\w+\b)
    |(\[[0-9,\-\s;]+\])
    |(\([A-Z][A-Za-z-]+(?:\s+et\s+al\.)?,?\s+\d{4}[a-z]?\))
    |(\b[A-Za-z]_\{?[A-Za-z0-9]+\}?\b)
    |(\b[A-Za-z]\^[A-Za-z0-9+\-]+\b)
    |(\b[A-Za-z]\s*[=<>]\s*[-+]?[A-Za-z0-9.]+\b)
    |(\b(?:p|P)\s*[<=>]\s*0?\.\d+\b)
    |(\bO\([^)]+\))
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Fragment:
    page_index: int
    bbox: tuple[float, float, float, float]
    text: str
    font_size: float
    color: tuple[float, float, float]


def parse_pages(spec: str | None, page_count: int) -> set[int] | None:
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            pages.update(range(start - 1, end))
        else:
            pages.add(int(part) - 1)
    invalid = [p + 1 for p in pages if p < 0 or p >= page_count]
    if invalid:
        raise ValueError(f"Invalid page number(s): {invalid}")
    return pages


def color_int_to_rgb(color: int) -> tuple[float, float, float]:
    r = ((color >> 16) & 255) / 255
    g = ((color >> 8) & 255) / 255
    b = (color & 255) / 255
    return (r, g, b)


def is_mostly_formula(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    letters = sum(ch.isalpha() for ch in stripped)
    math = sum(ch in MATH_CHARS for ch in stripped)
    digits = sum(ch.isdigit() for ch in stripped)
    spaces = sum(ch.isspace() for ch in stripped)
    visible = max(1, len(stripped) - spaces)
    if math / visible >= 0.12:
        return True
    if letters <= 2 and (digits + math) / visible >= 0.45:
        return True
    return False


def should_translate(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    if not re.search(r"[A-Za-z]{2,}", stripped):
        return False
    if is_mostly_formula(stripped):
        return False
    if stripped.isupper() and len(stripped.split()) <= 3:
        return False
    return True


def split_translatable_ranges(text: str) -> list[tuple[int, int]]:
    protected: list[tuple[int, int]] = []
    for match in PROTECTED_RE.finditer(text):
        protected.append((match.start(), match.end()))

    for index, char in enumerate(text):
        if char not in MATH_CHARS:
            continue
        if any(start <= index < end for start, end in protected):
            continue
        left = index
        if left > 0 and text[left - 1].isspace():
            probe = left - 1
            while probe > 0 and text[probe - 1].isspace():
                probe -= 1
            while probe > 0 and not text[probe - 1].isspace():
                probe -= 1
            left = probe
        else:
            while left > 0 and not text[left - 1].isspace():
                left -= 1

        right = index + 1
        while right < len(text):
            current = text[right]
            if current in ",;\n":
                break
            if current == "." and not (
                right > 0
                and right + 1 < len(text)
                and text[right - 1].isdigit()
                and text[right + 1].isdigit()
            ):
                break
            right += 1
        protected.append((left, right))

    protected.sort()
    merged_protected: list[tuple[int, int]] = []
    for start, end in protected:
        if not merged_protected or start > merged_protected[-1][1]:
            merged_protected.append((start, end))
        else:
            prev_start, prev_end = merged_protected[-1]
            merged_protected[-1] = (prev_start, max(prev_end, end))

    ranges: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged_protected:
        if cursor < start:
            ranges.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        ranges.append((cursor, len(text)))

    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        chunk = text[start:end]
        if should_translate(chunk):
            merged.append((start, end))
    return merged


def estimate_sub_bbox(
    bbox: tuple[float, float, float, float], start: int, end: int, total: int
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    width = x1 - x0
    total = max(total, 1)
    left = x0 + width * (start / total)
    right = x0 + width * (end / total)
    pad = min(1.2, max(0.2, width * 0.01))
    return (max(x0, left - pad), y0, min(x1, right + pad), y1)


def has_math_marker(text: str) -> bool:
    return any(char in MATH_CHARS for char in text) or bool(PROTECTED_RE.search(text))


def collect_fragments(
    doc: fitz.Document, page_filter: set[int] | None, translate_mixed_math: bool
) -> list[Fragment]:
    fragments: list[Fragment] = []
    for page_index, page in enumerate(doc):
        if page_filter is not None and page_index not in page_filter:
            continue
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not should_translate(text):
                        continue
                    if has_math_marker(text) and not translate_mixed_math:
                        continue
                    bbox = tuple(span["bbox"])
                    font_size = float(span.get("size", 9.0))
                    color = color_int_to_rgb(int(span.get("color", 0)))
                    for start, end in split_translatable_ranges(text):
                        fragment_text = text[start:end]
                        fragment_bbox = estimate_sub_bbox(bbox, start, end, len(text))
                        fragments.append(
                            Fragment(
                                page_index=page_index,
                                bbox=fragment_bbox,
                                text=fragment_text,
                                font_size=font_size,
                                color=color,
                            )
                        )
    return fragments


def batched(items: list[Fragment], size: int) -> Iterable[list[Fragment]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def parse_translation_payload(content: str, items: list[Fragment]) -> list[str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if isinstance(data, dict):
        for key in ("items", "results", "translations", "translated_items", "data", "output"):
            if key in data:
                data = data[key]
                break
        else:
            numeric_items = [
                {"id": int(key), "translation": value}
                for key, value in data.items()
                if str(key).isdigit()
            ]
            if numeric_items:
                data = numeric_items
            else:
                list_values = [value for value in data.values() if isinstance(value, list)]
                data = list_values[0] if list_values else []

    if isinstance(data, list) and all(isinstance(row, str) for row in data):
        data = [{"id": index, "translation": row} for index, row in enumerate(data)]

    normalized = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        translation = (
            row.get("translation")
            or row.get("translated_text")
            or row.get("translated")
            or row.get("text")
            or row.get("target")
        )
        if translation is None:
            continue
        row_id = row.get("id", row.get("index", index))
        normalized.append({"id": int(row_id), "translation": str(translation)})

    if len(normalized) == len(items):
        keys = {row["id"] for row in normalized}
        expected = set(range(len(items)))
        if not keys.intersection(expected):
            return [row["translation"] for row in normalized]
        if keys == set(range(1, len(items) + 1)):
            return [row["translation"] for row in normalized]

    data = normalized
    by_id = {int(row["id"]): str(row["translation"]) for row in data}
    missing = [i for i in range(len(items)) if i not in by_id]
    if missing:
        print(f"Warning: model omitted item id(s) {missing}; keeping source text.", file=sys.stderr)
    return [by_id.get(i, items[i].text) for i in range(len(items))]


def translate_batch(
    client: OpenAI, model: str, items: list[Fragment], max_retries: int
) -> list[str]:
    payload = [{"id": i, "text": item.text} for i, item in enumerate(items)]
    system = (
        "You are a precise academic paper translator. Translate English prose into "
        "Simplified Chinese. Preserve numbers, citations, variable names, symbols, "
        "URLs, emails, and any math-like text exactly. Return only valid JSON with "
        "double-quoted keys and strings. Do not include markdown."
    )
    user = {
        "task": (
            "Translate each item. Return a JSON array of objects. Each object must "
            "have exactly these keys: id, translation."
        ),
        "items": payload,
    }
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        )
        content = response.choices[0].message.content or ""
        try:
            return parse_translation_payload(content, items)
        except Exception as exc:
            last_error = exc
            print(
                f"Warning: could not parse model JSON on attempt {attempt + 1}; retrying.",
                file=sys.stderr,
            )
    print(
        f"Warning: giving up on batch after JSON parse failure: {last_error}. Keeping source text.",
        file=sys.stderr,
    )
    return [item.text for item in items]


def find_default_font() -> str | None:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def add_redactions(doc: fitz.Document, fragments: list[Fragment]) -> None:
    for fragment in fragments:
        page = doc[fragment.page_index]
        page.add_redact_annot(fitz.Rect(fragment.bbox), fill=(1, 1, 1))
    for page in doc:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)


def insert_translation(
    doc: fitz.Document,
    fragments: list[Fragment],
    translations: list[str],
    font_file: str | None,
    font_scale: float,
) -> None:
    font_name = "china-s"
    if font_file:
        font_name = "paper-cn"
        for page in doc:
            page.insert_font(fontname=font_name, fontfile=font_file)

    for fragment, translation in zip(fragments, translations, strict=True):
        page = doc[fragment.page_index]
        rect = fitz.Rect(fragment.bbox)
        rect.y0 -= 0.8
        rect.y1 += 1.8
        base_size = max(4.5, fragment.font_size * font_scale)
        written = False
        for step in range(8):
            size = max(4.0, base_size - step * 0.6)
            shape = page.new_shape()
            result = shape.insert_textbox(
                rect,
                translation,
                fontsize=size,
                fontname=font_name,
                color=fragment.color,
                align=fitz.TEXT_ALIGN_LEFT,
            )
            if result >= 0 or step == 7:
                shape.commit()
                written = True
                break
        if not written:
            page.insert_textbox(rect, translation, fontsize=4.0, fontname=font_name)


def translate_pdf(args: argparse.Namespace) -> None:
    input_path = Path(args.input_pdf)
    output_path = Path(args.output_pdf)
    if not input_path.exists():
        raise SystemExit(f"Input PDF not found: {input_path}")

    doc = fitz.open(input_path)
    page_filter = parse_pages(args.pages, doc.page_count)
    fragments = collect_fragments(doc, page_filter, args.translate_mixed_math)
    print(f"Pages: {doc.page_count}")
    print(f"Translatable fragments: {len(fragments)}")

    if args.dry_run:
        for sample in fragments[:20]:
            print(f"p{sample.page_index + 1}: {sample.text[:100]}")
        return

    translations: list[str] = []
    if args.mock_translation:
        translations = [f"译文：{fragment.text}" for fragment in fragments]
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit(
                "OPENAI_API_KEY is required unless you use --dry-run or --mock-translation."
            )

        model = args.model or os.getenv("OPENAI_MODEL", "gpt-4.1")
        client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)

        for index, batch in enumerate(batched(fragments, args.batch_size), start=1):
            print(f"Translating batch {index} ({len(batch)} fragments)...")
            translations.extend(translate_batch(client, model, batch, args.max_retries))

    font_file = args.font_file or find_default_font()
    if not font_file:
        print(
            "Warning: no CJK font file found. The built-in CJK font will be used; "
            "install Noto Sans CJK if Chinese text is not visible.",
            file=sys.stderr,
        )

    add_redactions(doc, fragments)
    insert_translation(doc, fragments, translations, font_file, args.font_scale)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path, garbage=4, deflate=True)
    print(f"Wrote {output_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", help="Source English PDF")
    parser.add_argument("output_pdf", help="Translated PDF output path")
    parser.add_argument("--pages", help="1-based page ranges, for example: 1-3,8")
    parser.add_argument("--model", help="OpenAI-compatible model name")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--font-file", help="CJK font file for translated text")
    parser.add_argument("--font-scale", type=float, default=0.82)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mock-translation",
        action="store_true",
        help="Write placeholder Chinese text without calling a model, useful for layout tests",
    )
    parser.add_argument(
        "--translate-mixed-math",
        action="store_true",
        help="Also translate spans that contain math-like text; higher coverage but less safe for formulas",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    translate_pdf(args)


if __name__ == "__main__":
    main()
