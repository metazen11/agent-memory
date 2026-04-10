#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FT_VENV="${FT_VENV:-$ROOT_DIR/.venv-finetune}"

python3 -m venv "$FT_VENV"
source "$FT_VENV/bin/activate"
python -m pip install --upgrade pip setuptools wheel

python -m pip install -r "$ROOT_DIR/fine-tune/requirements.txt"
python -m pip install -r "$ROOT_DIR/fine-tune/gguf/requirements.txt"

echo "fine-tune env ready: $FT_VENV"
echo "activate with: source $FT_VENV/bin/activate"
