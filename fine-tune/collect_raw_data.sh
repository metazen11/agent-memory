#!/usr/bin/env bash
set -euo pipefail

# Collect local logs into data/raw/ without mutating source logs.
# Usage:
#   bash fine-tune/collect_raw_data.sh
#   CLAUDE_LOG_DIR=~/.claude/projects bash fine-tune/collect_raw_data.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW_DIR="$ROOT_DIR/data/raw"
CLAUDE_LOG_DIR="${CLAUDE_LOG_DIR:-$HOME/.claude/projects}"

mkdir -p "$RAW_DIR/claude" "$RAW_DIR/anvil"

if [ -d "$CLAUDE_LOG_DIR" ]; then
  find "$CLAUDE_LOG_DIR" -type f -name "*.jsonl" -print0 | while IFS= read -r -d '' f; do
    rel="${f#$CLAUDE_LOG_DIR/}"
    dst="$RAW_DIR/claude/${rel//\//__}"
    cp "$f" "$dst"
  done
fi

if [ -d "$ROOT_DIR/.anvil/sessions" ]; then
  find "$ROOT_DIR/.anvil/sessions" -type f -name "*.json" -print0 | while IFS= read -r -d '' f; do
    cp "$f" "$RAW_DIR/anvil/$(basename "$f")"
  done
fi

echo "raw logs copied into $RAW_DIR"
