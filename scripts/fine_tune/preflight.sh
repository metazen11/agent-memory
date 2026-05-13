#!/bin/bash
# Pre-flight gate for training. Run before any DATASET_TIER=full job.
#
# Checks:
#  - disk free on the volume holding `models/` (we need ~30GB for full 3B run)
#  - Dropbox is NOT running (or offer to quit it)
#  - models/llama.cpp/build/bin/llama-cli works
#  - base model REVISION.txt exists for the slug
#  - dataset files exist
#
# Usage:
#   scripts/fine_tune/preflight.sh                       # default slug qwen2.5-3b-instruct
#   scripts/fine_tune/preflight.sh qwen2.5-7b-instruct
set -euo pipefail
cd "$(dirname "$0")/../.."

SLUG="${1:-qwen2.5-3b-instruct}"
MIN_FREE_GB="${MIN_FREE_GB:-30}"

ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$*"; FAIL=1; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

FAIL=0
echo "=== Pre-flight: $SLUG ==="

# Disk free
FREE_GB=$(df -Pg . | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
    fail "disk free: ${FREE_GB}GB (need ≥${MIN_FREE_GB}GB)"
else
    ok "disk free: ${FREE_GB}GB"
fi

# Dropbox (must be down for training)
if pgrep -f "Dropbox.app/Contents/MacOS/Dropbox\$" > /dev/null; then
    warn "Dropbox is running — will corrupt artifacts on sync. Quitting..."
    osascript -e 'tell application "Dropbox" to quit' || true
    sleep 3
    if pgrep -f "Dropbox.app/Contents/MacOS/Dropbox\$" > /dev/null; then
        fail "Dropbox still running after quit attempt"
    else
        ok "Dropbox quit"
    fi
else
    ok "Dropbox not running"
fi

# llama.cpp
if [ -x models/llama.cpp/build/bin/llama-cli ]; then
    ok "llama-cli present"
else
    fail "llama-cli not built at models/llama.cpp/build/bin/llama-cli"
fi
if [ -x models/llama.cpp/build/bin/llama-quantize ]; then
    ok "llama-quantize present"
else
    fail "llama-quantize not built"
fi

# Base model
REV="models/base/${SLUG}/REVISION.txt"
if [ -f "$REV" ]; then
    ok "base model: $(cat "$REV" | head -c 12)…"
else
    fail "base not downloaded: run scripts/fine_tune/download_base.py $SLUG"
fi

# Dataset
DATASET_DIR="data/processed/qwen25_tools/v1"
for f in train.chat.jsonl valid.chat.jsonl train.tiny.jsonl valid.tiny.jsonl MANIFEST.json; do
    if [ -f "$DATASET_DIR/$f" ]; then
        ok "dataset: $f"
    else
        fail "dataset missing: $DATASET_DIR/$f"
    fi
done

# Python venv
if [ -x ".venv-finetune/bin/python" ]; then
    ok "venv: .venv-finetune"
else
    fail "venv not found at .venv-finetune"
fi

echo
if [ "$FAIL" -eq 0 ]; then
    echo "PASS — pre-flight green."
    exit 0
else
    echo "FAIL — see failures above."
    exit 1
fi
