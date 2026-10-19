#!/usr/bin/env python3
"""MinerU-based PDF paper translation workflow.

This pipeline treats MinerU as the document parser. It translates only safe
reading-order text blocks from MinerU's content_list.json, preserves equations,
tables, images, and code blocks, then emits translated structured artifacts and
an optional block-level overlay PDF.
"""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Missing dependency: PyMuPDF. Run `python -m pip install -r requirements.txt`.") from exc

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Missing dependency: openai. Run `python -m pip install -r requirements.txt`.") from exc


TEXT_TYPES = {
    "text",
    "list",
    "header",
    "footer",
    "aside_text",
    "page_footnote",
}

CAPTION_FIELDS = {
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
    "table_caption",
    "table_footnote",
    "code_caption",
    "code_footnote",
}

PROTECT_RE = re.compile(
    r"""
    (\$\$.*?\$\$)
    |(\$[^$\n]+\$)
    |(\\\(.+?\\\))
    |(\\\[.+?\\\])
    |(https?://\S+)
    |(\b[\w.-]+@[\w.-]+\.\w+\b)
    |(\[[0-9,\-\s;]+\])
    |(\([A-Z][A-Za-z-]+(?:\s+et\s+al\.)?,?\s+\d{4}[a-z]?\))
    |(\b[A-Za-z]_\{?[A-Za-z0-9]+\}?\b)
    |(\b[A-Za-z]\^[A-Za-z0-9+\-]+\b)
    """,
    re.VERBOSE | re.S,
)


@dataclass
class TranslationUnit:
    item_index: int
    field_path: tuple[str | int, ...]
    source: str
    protected_source: str
    protected: dict[str, str]


BILINGUAL_SEPARATOR = "\n\n译文："


def run_mineru(
    pdf: Path,
    out_dir: Path,
    mineru_cmd: str,
    extra_args: list[str],
    backend: str,
    method: str,
    lang: str,
) -> None:
    exe = shutil.which(mineru_cmd)
    if not exe:
        raise SystemExit(
            f"`{mineru_cmd}` not found. Install MinerU first, or run this script with "
            "`--skip-parse --content-list path/to/*_content_list.json`."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-p",
        str(pdf),
        "-o",
        str(out_dir),
        "-b",
        backend,
        "-m",
        method,
        "-l",
        lang,
        *extra_args,
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def find_content_list(out_dir: Path, explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise SystemExit(f"content_list.json not found: {explicit}")
        return explicit
    candidates = sorted(out_dir.rglob("*_content_list.json"))
    if not candidates:
        candidates = sorted(out_dir.rglob("content_list.json"))
    if not candidates:
        raise SystemExit(
            f"No MinerU content list found under {out_dir}. Expected *_content_list.json."
        )
    return candidates[0]


def protect_text(text: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        token = f"__PROTECTED_{len(protected)}__"
        protected[token] = match.group(0)
        return token

    return PROTECT_RE.sub(repl, text), protected


def restore_text(text: str, protected: dict[str, str]) -> str:
    restored = text
    for token, value in protected.items():
        restored = restored.replace(token, value)
    return restored


def looks_translatable(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if not re.search(r"[A-Za-z]{2,}", stripped):
        return False
    if stripped.startswith("$$") and stripped.endswith("$$"):
        return False
    return True


def get_path(item: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    value: Any = item
    for key in path:
        value = value[key]
    return value


def set_path(item: dict[str, Any], path: tuple[str | int, ...], value: Any) -> None:
    target: Any = item
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def metadata_key(path: tuple[str | int, ...], suffix: str) -> str:
    safe = "_".join(str(part) for part in path)
    return f"_{safe}_{suffix}"


def compose_output_text(source: str, translation: str, bilingual: bool) -> str:
    if not bilingual:
        return translation
    return f"{source}{BILINGUAL_SEPARATOR}{translation}"


def record_translation_metadata(
    item: dict[str, Any], unit: TranslationUnit, translation: str, bilingual: bool
) -> None:
    item[metadata_key(unit.field_path, "source_text")] = unit.source
    item[metadata_key(unit.field_path, "translated_text")] = translation
    item[metadata_key(unit.field_path, "bilingual")] = bilingual


def gather_units(content: list[dict[str, Any]]) -> list[TranslationUnit]:
    units: list[TranslationUnit] = []
    for item_index, item in enumerate(content):
        item_type = item.get("type")
        field_paths: list[tuple[str | int, ...]] = []

        if item_type in TEXT_TYPES:
            for field in ("text", "list_body"):
                if isinstance(item.get(field), str):
                    field_paths.append((field,))
            if isinstance(item.get("list_items"), list):
                for i, entry in enumerate(item["list_items"]):
                    if isinstance(entry, str):
                        field_paths.append(("list_items", i))

        for field in CAPTION_FIELDS:
            value = item.get(field)
            if isinstance(value, str):
                field_paths.append((field,))
            elif isinstance(value, list):
                for i, entry in enumerate(value):
                    if isinstance(entry, str):
                        field_paths.append((field, i))

        for field_path in field_paths:
            source = get_path(item, field_path)
            if not looks_translatable(source):
                continue
            protected_source, protected = protect_text(source)
            units.append(
                TranslationUnit(
                    item_index=item_index,
                    field_path=field_path,
                    source=source,
                    protected_source=protected_source,
                    protected=protected,
                )
            )
    return units


def parse_translation_response(content: str, expected: int) -> list[str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.S)
        if not match:
            raise
        data = json.loads(match.group(0))

    if isinstance(data, dict):
        for key in ("items", "results", "translations", "data", "output"):
            if isinstance(data.get(key), list):
                data = data[key]
                break

    if isinstance(data, list) and all(isinstance(row, str) for row in data):
        return [str(row) for row in data]

    if not isinstance(data, list):
        raise ValueError("translation response is not a list")

    rows: list[tuple[int, str]] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            continue
        text = row.get("translation") or row.get("translated_text") or row.get("text")
        if text is None:
            continue
        row_id = int(row.get("id", row.get("index", index)))
        rows.append((row_id, str(text)))

    by_id = {row_id: text for row_id, text in rows}
    if set(by_id) == set(range(expected)):
        return [by_id[i] for i in range(expected)]
    if set(by_id) == set(range(1, expected + 1)):
        return [by_id[i] for i in range(1, expected + 1)]
    if len(rows) == expected:
        return [text for _, text in rows]
    raise ValueError(f"expected {expected} translations, got {len(rows)}")


def translate_batch(
    client: OpenAI,
    model: str,
    units: list[TranslationUnit],
    max_retries: int,
    request_timeout: float,
) -> list[str]:
    payload = [{"id": i, "text": unit.protected_source} for i, unit in enumerate(units)]
    system = (
        "You translate academic paper prose from English to Simplified Chinese. "
        "Keep every token like __PROTECTED_0__ exactly unchanged. Do not translate "
        "LaTeX, citations, URLs, emails, variable names, or equations. Return only "
        "valid JSON."
    )
    user = {
        "task": (
            "Translate each item. Return a JSON array of objects with exactly these "
            "keys: id, translation."
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
            timeout=request_timeout,
        )
        raw = response.choices[0].message.content or ""
        try:
            translated = parse_translation_response(raw, len(units))
            return [
                restore_text(text, unit.protected)
                for text, unit in zip(translated, units, strict=True)
            ]
        except Exception as exc:
            last_error = exc
            print(f"Warning: bad translation JSON attempt {attempt + 1}: {exc}", file=sys.stderr)
    print(f"Warning: keeping source batch after retries: {last_error}", file=sys.stderr)
    return [unit.source for unit in units]


def translate_content(
    content: list[dict[str, Any]],
    model: str,
    batch_size: int,
    max_retries: int,
    mock: bool,
    bilingual: bool,
    cache_dir: Path | None,
    request_timeout: float,
) -> tuple[list[dict[str, Any]], int]:
    translated = json.loads(json.dumps(content, ensure_ascii=False))
    units = gather_units(translated)
    print(f"Translatable MinerU units: {len(units)}", flush=True)
    if mock:
        for unit in units:
            item = translated[unit.item_index]
            result = unit.source
            record_translation_metadata(item, unit, result, bilingual)
            set_path(item, unit.field_path, compose_output_text(unit.source, result, bilingual))
        return translated, len(units)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required unless --mock is used.")
    client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    for start in range(0, len(units), batch_size):
        batch = units[start : start + batch_size]
        batch_payload = {
            "model": model,
            "sources": [unit.source for unit in batch],
            "protected_sources": [unit.protected_source for unit in batch],
        }
        cache_path: Path | None = None
        if cache_dir:
            digest = hashlib.sha256(
                json.dumps(batch_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            cache_path = cache_dir / f"batch-{start + 1:05d}-{digest[:16]}.json"

        if cache_path and cache_path.exists():
            results = json.loads(cache_path.read_text(encoding="utf-8"))
            print(
                f"Loaded cached units {start + 1}-{start + len(batch)}.",
                flush=True,
            )
        else:
            print(f"Translating units {start + 1}-{start + len(batch)}...", flush=True)
            results = translate_batch(client, model, batch, max_retries, request_timeout)
            if cache_path:
                cache_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        for unit, result in zip(batch, results, strict=True):
            item = translated[unit.item_index]
            record_translation_metadata(item, unit, result, bilingual)
            set_path(item, unit.field_path, compose_output_text(unit.source, result, bilingual))
    return translated, len(units)


def resolve_asset(base_dir: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base_dir / p


def content_to_markdown(content: list[dict[str, Any]], asset_base: Path) -> str:
    lines: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            level = int(item.get("text_level") or 0)
            if level > 0:
                lines.append(f"{'#' * min(level, 6)} {text}")
            else:
                lines.append(text)
        elif item_type == "list":
            text = item.get("text") or item.get("list_body")
            if text:
                lines.append(text)
            for entry in item.get("list_items", []):
                lines.append(f"- {entry}")
        elif item_type == "equation":
            lines.append(item.get("text", ""))
        elif item_type in {"image", "chart"}:
            for caption in item.get(f"{item_type}_caption", []) or item.get("image_caption", []):
                lines.append(caption)
            if item.get("img_path"):
                img = resolve_asset(asset_base, item["img_path"])
                lines.append(f"![{item_type}]({img})")
            for footnote in item.get(f"{item_type}_footnote", []) or item.get("image_footnote", []):
                lines.append(footnote)
        elif item_type == "table":
            for caption in item.get("table_caption", []):
                lines.append(caption)
            lines.append(item.get("table_body") or item.get("text") or "")
            for footnote in item.get("table_footnote", []):
                lines.append(footnote)
        elif item_type == "code":
            for caption in item.get("code_caption", []):
                lines.append(caption)
            lines.append("```")
            lines.append(item.get("code_body", ""))
            lines.append("```")
            for footnote in item.get("code_footnote", []):
                lines.append(footnote)
        else:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def content_to_html(content: list[dict[str, Any]], asset_base: Path) -> str:
    body: list[str] = []
    for item in content:
        item_type = item.get("type")
        classes = f"block {html.escape(str(item_type))}"
        if item_type == "text":
            text = html.escape(item.get("text", ""))
            level = int(item.get("text_level") or 0)
            if level > 0:
                tag = f"h{min(level, 6)}"
                body.append(f"<{tag} class='{classes}'>{text}</{tag}>")
            else:
                body.append(f"<p class='{classes}'>{text}</p>")
        elif item_type == "equation":
            body.append(f"<div class='{classes} math'>{html.escape(item.get('text', ''))}</div>")
        elif item_type == "table":
            captions = "".join(f"<figcaption>{html.escape(c)}</figcaption>" for c in item.get("table_caption", []))
            table = item.get("table_body") or html.escape(item.get("text", ""))
            notes = "".join(f"<p class='note'>{html.escape(n)}</p>" for n in item.get("table_footnote", []))
            body.append(f"<figure class='{classes}'>{captions}{table}{notes}</figure>")
        elif item_type in {"image", "chart"}:
            captions = item.get(f"{item_type}_caption") or item.get("image_caption") or []
            notes = item.get(f"{item_type}_footnote") or item.get("image_footnote") or []
            fig = "".join(f"<figcaption>{html.escape(c)}</figcaption>" for c in captions)
            if item.get("img_path"):
                img = resolve_asset(asset_base, item["img_path"])
                fig += f"<img src='{html.escape(str(img))}' alt='{html.escape(item_type)}'>"
            fig += "".join(f"<p class='note'>{html.escape(n)}</p>" for n in notes)
            body.append(f"<figure class='{classes}'>{fig}</figure>")
        elif item_type == "list":
            entries = item.get("list_items")
            if isinstance(entries, list) and entries:
                lis = "".join(f"<li>{html.escape(str(entry))}</li>" for entry in entries)
                body.append(f"<ul class='{classes}'>{lis}</ul>")
            else:
                body.append(f"<p class='{classes}'>{html.escape(item.get('text') or item.get('list_body') or '')}</p>")
        elif item_type == "code":
            body.append(f"<pre class='{classes}'><code>{html.escape(item.get('code_body', ''))}</code></pre>")
        else:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                body.append(f"<p class='{classes}'>{html.escape(text)}</p>")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Translated Paper</title>
  <style>
    body {{ margin: 0; background: #f3f4f6; color: #111827; font-family: "PingFang SC", "Noto Sans CJK SC", sans-serif; }}
    main {{ max-width: 920px; margin: 32px auto; padding: 42px 56px; background: white; box-shadow: 0 1px 8px rgba(0,0,0,.08); }}
    p {{ line-height: 1.78; margin: 0 0 12px; }}
    h1, h2, h3, h4 {{ color: #0f3b75; margin: 24px 0 12px; }}
    figure {{ margin: 18px 0; }}
    figcaption {{ color: #374151; font-size: 14px; margin-bottom: 8px; }}
    img {{ max-width: 100%; height: auto; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    td, th {{ border: 1px solid #d1d5db; padding: 6px 8px; vertical-align: top; }}
    .math {{ white-space: pre-wrap; font-family: "Times New Roman", serif; text-align: center; margin: 14px 0; }}
    .note {{ color: #4b5563; font-size: 13px; }}
    .header, .footer, .page_number {{ color: #6b7280; font-size: 12px; }}
  </style>
</head>
<body><main>
{chr(10).join(body)}
</main></body></html>
"""


def normalized_bbox_to_rect(page: fitz.Page, bbox: list[float]) -> fitz.Rect:
    width = page.rect.width
    height = page.rect.height
    x0, y0, x1, y1 = bbox
    return fitz.Rect(x0 / 1000 * width, y0 / 1000 * height, x1 / 1000 * width, y1 / 1000 * height)


def default_font_file() -> str | None:
    for candidate in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/NotoSansCJK-Regular.ttc",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def write_overlay_pdf(
    source_pdf: Path,
    content: list[dict[str, Any]],
    out_pdf: Path,
    font_file: str | None,
    font_scale: float,
) -> None:
    doc = fitz.open(source_pdf)
    font_name = "china-s"
    font_kwargs: dict[str, str] = {}
    if font_file:
        font_name = "paper-cn"
        font_kwargs["fontfile"] = font_file

    overlay_items: list[tuple[int, fitz.Rect, str, int]] = []
    for item in content:
        item_type = item.get("type")
        if item_type not in TEXT_TYPES:
            continue
        if not isinstance(item.get("bbox"), list) or item.get("page_idx") is None:
            continue
        text = item.get("text") or item.get("list_body")
        if not isinstance(text, str) or not text.strip():
            continue
        page_idx = int(item["page_idx"])
        if not (0 <= page_idx < doc.page_count):
            continue
        rect = normalized_bbox_to_rect(doc[page_idx], item["bbox"])
        overlay_items.append((page_idx, rect, text, int(item.get("text_level") or 0)))
        doc[page_idx].add_redact_annot(rect, fill=(1, 1, 1))

    for page in doc:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

    for page_idx, rect, text, level in overlay_items:
        page = doc[page_idx]
        size = 7.2 * font_scale
        if level == 1:
            size = 13 * font_scale
        elif level == 2:
            size = 10.5 * font_scale
        elif level >= 3:
            size = 9.0 * font_scale
        page.insert_textbox(
            rect + (-0.5, -0.5, 0.5, 0.5),
            text,
            fontsize=max(4.5, size),
            fontname=font_name,
            color=(0, 0, 0),
            align=fitz.TEXT_ALIGN_LEFT,
            **font_kwargs,
        )

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf, garbage=4, deflate=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="Source PDF")
    parser.add_argument("--out-dir", default="output/mineru-workflow")
    parser.add_argument("--content-list", help="Existing MinerU *_content_list.json")
    parser.add_argument("--skip-parse", action="store_true", help="Do not run MinerU; use an existing content list")
    parser.add_argument("--mineru-cmd", default="mineru")
    parser.add_argument("--mineru-extra", nargs="*", default=[], help="Extra args passed to MinerU after -p/-o")
    parser.add_argument("--mineru-backend", default="pipeline", help="MinerU backend passed as -b")
    parser.add_argument("--mineru-method", default="auto", help="MinerU parse method passed as -m")
    parser.add_argument("--mineru-lang", default="en", help="MinerU language passed as -l")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4.1"))
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=90.0, help="Per translation request timeout in seconds")
    parser.add_argument("--cache-dir", help="Directory for cached translation batches; defaults to OUT_DIR/.translation_cache")
    parser.add_argument("--mock", action="store_true", help="Do not call a model; prefix text with a marker")
    parser.add_argument("--bilingual", action="store_true", help="Put source text before translated text in every translated block")
    parser.add_argument("--overlay-pdf", action="store_true", help="Create block-level translated overlay PDF")
    parser.add_argument("--font-file")
    parser.add_argument("--font-scale", type=float, default=0.92)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    pdf = Path(args.pdf).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")

    if not args.skip_parse:
        run_mineru(
            pdf,
            out_dir / "mineru",
            args.mineru_cmd,
            args.mineru_extra,
            args.mineru_backend,
            args.mineru_method,
            args.mineru_lang,
        )

    content_path = find_content_list(out_dir, Path(args.content_list).expanduser().resolve() if args.content_list else None)
    print(f"Using MinerU content list: {content_path}")
    content = json.loads(content_path.read_text(encoding="utf-8"))
    if not isinstance(content, list):
        raise SystemExit("MinerU content list must be a JSON array.")

    translated, count = translate_content(
        content,
        args.model,
        args.batch_size,
        args.max_retries,
        args.mock,
        args.bilingual,
        Path(args.cache_dir).expanduser().resolve() if args.cache_dir else out_dir / ".translation_cache",
        args.request_timeout,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    translated_json = out_dir / "translated_content_list.json"
    translated_md = out_dir / "translated.md"
    translated_html = out_dir / "translated.html"
    translated_json.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
    translated_md.write_text(content_to_markdown(translated, content_path.parent), encoding="utf-8")
    translated_html.write_text(content_to_html(translated, content_path.parent), encoding="utf-8")
    print(f"Wrote {translated_json}")
    print(f"Wrote {translated_md}")
    print(f"Wrote {translated_html}")

    if args.overlay_pdf:
        out_pdf = out_dir / "translated_overlay.pdf"
        write_overlay_pdf(
            pdf,
            translated,
            out_pdf,
            args.font_file or default_font_file(),
            args.font_scale,
        )
        print(f"Wrote {out_pdf}")
    print(f"Translated units: {count}")


if __name__ == "__main__":
    main()
