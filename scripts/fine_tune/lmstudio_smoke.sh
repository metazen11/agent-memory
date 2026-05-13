#!/bin/bash
# LM Studio integration smoke test.
#
# Reusable across any GGUF in models/gguf/. Drops the GGUF into LM Studio's
# model directory, prompts the user to load it + register tools, then runs
# the validator against LM Studio's OpenAI-compatible server.
#
# Usage:
#   scripts/fine_tune/lmstudio_smoke.sh [gguf-path] [min-parse-rate]
# Defaults:
#   gguf-path:        models/gguf/qwen2.5-3b-toolcalls-q4km.gguf
#   min-parse-rate:   0.1

set -euo pipefail
cd "$(dirname "$0")/../.."

GGUF="${1:-models/gguf/qwen2.5-3b-toolcalls-q4km.gguf}"
MIN_RATE="${2:-0.1}"

if [ ! -f "$GGUF" ]; then
    echo "FAIL: $GGUF not found"
    exit 1
fi

# LM Studio scans ~/.lmstudio/models/<publisher>/<repo>/  (NOT ~/.cache/lm-studio).
# Use "mz" as the publisher namespace for locally-trained models.
LMS_DIR="$HOME/.lmstudio/models/mz/qwen25-toolcalls"
echo "Copying GGUF -> $LMS_DIR/"
mkdir -p "$LMS_DIR"
cp -v "$GGUF" "$LMS_DIR/"

cat <<EOF

============================================================
LM Studio manual steps:
  1. Open LM Studio
  2. Load 'qwen25-toolcalls' from My Models
  3. Enable 'Local Server' tab -> 'Start Server' (default port 1234)
  4. Confirm http://localhost:1234/v1/models lists the model
============================================================

Press Enter when ready to run validator, or Ctrl-C to abort.
EOF
read -r

echo "Probing server..."
if ! curl -sf http://localhost:1234/v1/models > /dev/null; then
    echo "FAIL: localhost:1234 not reachable. Start LM Studio's Local Server."
    exit 2
fi

MODEL_NAME="$(curl -s http://localhost:1234/v1/models | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')"
echo "Detected model: $MODEL_NAME"

echo "Running validator (min-parse-rate=$MIN_RATE)..."
.venv-finetune/bin/python scripts/fine_tune/validate_tool_calls.py \
    --backend openai \
    --base-url http://localhost:1234/v1 \
    --model "$MODEL_NAME" \
    --min-parse-rate "$MIN_RATE"
