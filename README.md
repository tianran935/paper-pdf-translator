# Paper PDF Translator

Translate English academic PDFs into bilingual Chinese Markdown and PDF while preserving equations, figures, tables, and reading order. The pipeline uses [MinerU](https://github.com/opendatalab/MinerU) for document parsing and any OpenAI-compatible API for translation.

## What it produces

Each translated paper leaves only two files in its output directory:

- `中文_<original-name>.pdf`: bilingual reading PDF with formulas and figures retained
- `中文_<original-name>.md`: bilingual Markdown

Temporary MinerU data, caches, images, TeX files, and render artifacts are removed automatically after a successful run.

## Requirements

- macOS or Linux with `zsh`
- Python 3.10+
- XeLaTeX (used to build the readable PDF)
- An OpenAI-compatible API key

On macOS, XeLaTeX can be installed with MacTeX or BasicTeX:

```bash
brew install --cask basictex
```

## Install

```bash
git clone https://github.com/tianran935/paper-pdf-translator.git
cd paper-pdf-translator
./install.sh
```

The installer creates `.venv`, installs Python dependencies including MinerU, and links `translate` into `~/.local/bin`. Make sure that directory is on your `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Add that line to `~/.zshrc` if needed, then open a new shell.

## Configure

Set a standard OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="gpt-4.1"
export OPENAI_BASE_URL="https://your-provider.example/v1"  # optional
```

Alibaba Cloud/Qwen-style variable names are also supported:

```bash
export QWEN_API_KEY="your-api-key"
export QWEN_MODEL="qwen-plus"
export QWEN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

For persistent configuration, put these exports in `~/.zshrc`. Never commit API keys to this repository.

## Use

Translate one PDF:

```bash
translate /path/to/paper.pdf /path/to/output
```

Translate every PDF directly inside a directory:

```bash
translate /path/to/input-directory /path/to/output-directory
```

Directory mode creates one subdirectory per PDF. It does not recurse into nested input directories.

Optional MinerU settings can be overridden with environment variables:

```bash
MINERU_BACKEND=pipeline MINERU_METHOD=auto MINERU_LANG=en translate input.pdf output
```

## How it works

1. MinerU parses the PDF into ordered document blocks.
2. Only natural-language text, captions, and notes are sent for translation.
3. Equations, code, tables, figures, URLs, citations, and identifiers are protected.
4. A bilingual Markdown file and readable PDF are generated.
5. Intermediate files are deleted.

The legacy direct PDF text-overlay implementation remains in `tools/translate_pdf.py` for reference, but the MinerU workflow is the supported path.

## Limitations

- The first MinerU run may download model files and can take a while.
- Complex scans or unusual layouts can still require manual review.
- Translation API usage may incur provider charges.
- Output PDF quality depends on a working XeLaTeX installation and available CJK fonts.

## License

MIT. See [LICENSE](LICENSE).
