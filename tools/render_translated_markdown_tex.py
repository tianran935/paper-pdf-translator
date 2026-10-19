#!/usr/bin/env python3
"""Render translated Markdown with LaTeX math to a uniform-font XeLaTeX PDF."""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
from pathlib import Path


SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_text(text: str) -> str:
    out = []
    for ch in text:
        out.append(SPECIALS.get(ch, ch))
    return "".join(out)


def escape_text_with_urls(text: str) -> str:
    parts: list[str] = []
    pos = 0
    url_re = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
    for match in re.finditer(url_re, text):
        parts.append(escape_text(text[pos : match.start()]))
        url = match.group(0).rstrip(".,;)")
        suffix = match.group(0)[len(url) :]
        parts.append(r"\url{" + url.replace("\\", "") + "}")
        parts.append(escape_text(suffix))
        pos = match.end()
    parts.append(escape_text(text[pos:]))
    return "".join(parts)


def protect_common_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = text.replace("\ufffd", "")
    text = text.replace("☉", "")
    text = text.replace("©", "(c)")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("′", "'").replace("ˆ", "^")
    text = text.replace("∆", r"$\Delta$")
    text = text.replace("σ", r"$\sigma$")
    text = text.replace("θ", r"$\theta$")
    text = text.replace("α", r"$\alpha$")
    text = text.replace(r"\_", "_")
    text = text.replace(r"\#", "#")
    text = text.replace(r"\&", "&")
    text = text.replace(r"\*", "*")
    text = text.replace(r"\`", "`")
    text = text.replace("https:// ", "https://").replace("http:// ", "http://")
    text = text.replace("•", "·")
    text = text.replace("–", "--")
    text = text.replace("—", "---")
    text = text.replace("≥", r"$\geq$")
    text = text.replace("≤", r"$\leq$")
    text = text.replace("×", r"$\times$")
    return text


def split_math_segments(line: str) -> list[tuple[bool, str]]:
    segments: list[tuple[bool, str]] = []
    i = 0
    while i < len(line):
        start = line.find("$", i)
        if start < 0:
            segments.append((False, line[i:]))
            break
        if start > i:
            segments.append((False, line[i:start]))
        end = line.find("$", start + 1)
        if end < 0:
            segments.append((False, line[start:]))
            break
        math = line[start + 1 : end].strip()
        if math:
            segments.append((True, math))
        i = end + 1
    return segments


def clean_math(math: str) -> str:
    math = math.replace(r"\operatorname { o r }", r"\operatorname{or}")
    math = math.replace(r"\mathscr", r"\mathcal")
    if r"\mathrm { d e t } \left( { { \begin{array} { c }" in math:
        return (
            r"\begin{array}{c}"
            r"\det\left(\begin{array}{cc}"
            r"e_{1,1} & e_{1,2}\\"
            r"1-e_{2,1} & 1-e_{2,2}"
            r"\end{array}\right)\geq 0,"
            r"\quad \mathrm{or\ equivalently,}\\[0.35em]"
            r"\dfrac{e_{1,1}}{1-e_{2,1}}\geq"
            r"\dfrac{e_{1,2}}{1-e_{2,2}}."
            r"\end{array}"
        )
    if r"M _ { n } = ( \begin{array}" in math:
        math = math.replace(
            r"M _ { n } = ( \begin{array}",
            r"M _ { n } = \left( \begin{array}",
        )
        math = math.replace(
            r"\begin{array} { c c c c c c c c c }",
            r"\begin{array} { c c c c c c c c c c c c }",
        )
        if math.rstrip().endswith(r"\end{array}"):
            math = math.rstrip() + r" \right)"
    math = math.replace(
        r"\begin{array} { r l }",
        r"\begin{array} { r l l l l }",
    )
    math = re.sub(r"\\scriptstyle\s+", "", math)
    math = re.sub(r"\\textstyle\s+", "", math)
    return math


def line_to_latex(line: str) -> str:
    protected = protect_common_text(line)
    parts = []
    for is_math, segment in split_math_segments(protected):
        if is_math:
            parts.append(f"${clean_math(segment)}$")
        else:
            parts.append(escape_text_with_urls(segment))
    return "".join(parts)


def html_table_to_text_rows(table_html: str) -> list[str]:
    rows: list[str] = []
    for row_match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", table_html, re.I | re.S):
        row_html = row_match.group(1)
        cells: list[str] = []
        for cell_match in re.finditer(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row_html, re.I | re.S):
            cell = re.sub(r"<[^>]+>", " ", cell_match.group(1))
            cell = html.unescape(re.sub(r"\s+", " ", cell)).strip()
            if cell:
                cells.append(cell)
        if cells:
            rows.append(" | ".join(cells))
    return rows


def markdown_to_tex(markdown: str) -> str:
    body: list[str] = []
    in_source_block = False
    in_display_math = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line == "$$":
            if in_display_math:
                body.append(r"\]")
                in_display_math = False
            else:
                if in_source_block:
                    body.append(r"\end{sourceblock}")
                    in_source_block = False
                body.append(r"\[")
                in_display_math = True
            continue
        if in_display_math:
            body.append(clean_math(line))
            continue
        if not line:
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append("")
            continue
        if line.lower().startswith("<table"):
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            rows = html_table_to_text_rows(line)
            if rows:
                body.append(r"\begin{sourceblock}")
                body.append(r"\footnotesize")
                for row in rows:
                    body.append(line_to_latex(row))
                    body.append("")
                body.append(r"\end{sourceblock}")
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            alt_text = image_match.group(1).strip() or "image"
            image_path = Path(image_match.group(2))
            if image_path.exists():
                body.append(r"\begin{center}")
                body.append(
                    r"\includegraphics[width=0.86\linewidth,height=0.62\textheight,keepaspectratio]{"
                    + image_path.as_posix().replace("\\", "/")
                    + "}"
                )
                body.append(r"\end{center}")
            else:
                body.append(r"{\footnotesize [missing image: " + line_to_latex(alt_text) + r"]}")
            continue
        if line.startswith("译文："):
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append(r"\begin{translationblock}")
            text = line.removeprefix("译文：").strip()
            if text:
                body.append(line_to_latex(text))
            body.append(r"\end{translationblock}")
            continue
        if line.startswith("# "):
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append(r"\section*{" + line_to_latex(line[2:].strip()) + "}")
        elif line.startswith("## "):
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append(r"\subsection*{" + line_to_latex(line[3:].strip()) + "}")
        elif line.startswith("### "):
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append(r"\subsubsection*{" + line_to_latex(line[4:].strip()) + "}")
        elif line.isdigit() and len(line) == 4:
            if in_source_block:
                body.append(r"\end{sourceblock}")
                in_source_block = False
            body.append(r"{\footnotesize " + escape_text(line) + r"}")
        else:
            if not in_source_block:
                body.append(r"\begin{sourceblock}")
                in_source_block = True
            body.append(line_to_latex(line))
            body.append("")
    if in_source_block:
        body.append(r"\end{sourceblock}")
    if in_display_math:
        body.append(r"\]")
    return "\n".join(body)


def build_document(markdown: str) -> str:
    body = markdown_to_tex(markdown)
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
\hypersetup{{colorlinks=true,linkcolor=black,urlcolor=blue}}
\linespread{{1.12}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0.62em}}
\newenvironment{{sourceblock}}{{\begingroup\small\color{{black!64}}\setlength{{\parskip}}{{0.35em}}}}{{\par\endgroup}}
\newenvironment{{translationblock}}{{\begingroup\setlength{{\parskip}}{{0.35em}}\textbf{{译文：}}\ignorespaces}}{{\par\endgroup}}
\sloppy
\begin{{document}}
{body}
\end{{document}}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("markdown")
    parser.add_argument("output_pdf")
    parser.add_argument("--work-dir", default="tmp/tex-render")
    args = parser.parse_args()

    xelatex = shutil.which("xelatex")
    if not xelatex:
        raise SystemExit("xelatex not found.")

    markdown_path = Path(args.markdown)
    output_pdf = Path(args.output_pdf)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    tex = build_document(markdown_path.read_text(encoding="utf-8"))
    tex_path = work_dir / "translated.tex"
    tex_path.write_text(tex, encoding="utf-8")

    for _ in range(2):
        subprocess.run(
            [xelatex, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=work_dir,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(work_dir / "translated.pdf", output_pdf)
    print(output_pdf)


if __name__ == "__main__":
    main()
