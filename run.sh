#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -x ".venv/bin/python" ]]; then
  VENV_DIR=".venv"
  PYTHON_BIN=".venv/bin/python"
elif [[ -x "venv/bin/python" ]]; then
  VENV_DIR="venv"
  PYTHON_BIN="venv/bin/python"
else
  PYTHON_CMD="${PYTHON_CMD:-python3}"
  if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    PYTHON_CMD="python"
  fi

  if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    echo "[!] Python 3 was not found in PATH."
    exit 1
  fi

  VENV_DIR=".venv"
  "$PYTHON_CMD" -m venv "$VENV_DIR"
  PYTHON_BIN="$VENV_DIR/bin/python"
  "$PYTHON_BIN" -m pip install --upgrade pip
fi

REQ_FILE="requirements.txt"
REQ_STAMP="$VENV_DIR/.requirements.sha256"
CURRENT_HASH="$("$PYTHON_BIN" -c "from pathlib import Path; import hashlib; print(hashlib.sha256(Path('$REQ_FILE').read_bytes()).hexdigest())")"
SAVED_HASH=""
if [[ -f "$REQ_STAMP" ]]; then
  SAVED_HASH="$(cat "$REQ_STAMP")"
fi

if [[ "$CURRENT_HASH" != "$SAVED_HASH" ]]; then
  "$PYTHON_BIN" -m pip install --upgrade -r "$REQ_FILE"
  printf '%s' "$CURRENT_HASH" > "$REQ_STAMP"
fi

exec "$PYTHON_BIN" main.py "$@"
