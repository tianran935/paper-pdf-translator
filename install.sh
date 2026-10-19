#!/bin/zsh
set -euo pipefail

SCRIPT_PATH="${0:A}"
PROJECT_DIR="${SCRIPT_PATH:h}"
VENV_DIR="$PROJECT_DIR/.venv"
USER_BIN="$HOME/.local/bin"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$USER_BIN"
ln -sfn "$PROJECT_DIR/translate" "$USER_BIN/translate"

echo
echo "Installed successfully."
echo "Command: $USER_BIN/translate"
if [[ ":$PATH:" != *":$USER_BIN:"* ]]; then
  echo "Add this line to ~/.zshrc:"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
fi
echo "Run: translate --help"
