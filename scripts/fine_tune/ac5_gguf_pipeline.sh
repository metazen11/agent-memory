#!/bin/bash
# AC5 runbook (issue #55): adapter -> merge -> GGUF Q4_K_M -> LM Studio -> tool-call validate.
#
# Runs the full local packaging step end-to-end once the HF Jobs training run
# reaches SUCCESS and the LoRA adapter is on the Hub. Fully unattended: uses
# `lms load` to register the model instead of the manual GUI step.
#
# Usage:
#   scripts/fine_tune/ac5_gguf_pipeline.sh
#
# Env overrides:
#   MODEL_REPO   Hub repo holding the adapter (default WFCA/qwen3-4b-toolcalls-v5-pilot)
#   BASE_MODEL   Base model to merge into      (default Qwen/Qwen3-4B)
#   MIN_RATE     Min tool-call parse rate gate (default 0.1)

set -euo pipefail
cd "$(dirname "$0")/../.."

MODEL_REPO="${MODEL_REPO:-WFCA/qwen3-4b-toolcalls-v5-pilot}"
BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B}"
MIN_RATE="${MIN_RATE:-0.1}"

WORK="$(pwd)/models/ac5-work"
ADAPTER_DIR="$WORK/adapter"
MERGED_DIR="$WORK/merged"
GGUF_F16="$WORK/qwen3-4b-toolcalls-v5-pilot-f16.gguf"
GGUF_Q4="models/gguf/qwen3-4b-toolcalls-v5-pilot-q4km.gguf"
LMS_DIR="$HOME/.lmstudio/models/mz/qwen3-4b-toolcalls-v5-pilot"

# Use .venv-finetune for every Python step. It is the genuine local working venv
# (healthy pip, peft/torch/transformers/gguf/hf_hub all present). NOTE: the plain
# .venv here is a corrupted Dropbox-mirror copy — its `pip` targets the Dropbox
# site-packages while its `python` reads the local prefix, so installs silently
# don't take. Do not use .venv for these steps.
VENV="$(pwd)/.venv-finetune/bin/python"

echo "== AC5 step 1/6: download adapter from $MODEL_REPO =="
"$VENV" - <<PY
from huggingface_hub import snapshot_download
p = snapshot_download(repo_id="$MODEL_REPO", local_dir="$ADAPTER_DIR",
                      allow_patterns=["adapter_*", "*.json", "tokenizer*", "*.model"])
print("downloaded ->", p)
PY

echo "== AC5 step 2/6: merge LoRA into $BASE_MODEL =="
"$VENV" fine-tune/gguf/merge_lora_hf.py \
    --base-model "$BASE_MODEL" \
    --lora-adapter "$ADAPTER_DIR" \
    --output-dir "$MERGED_DIR"

echo "== AC5 step 3/6: convert merged HF -> GGUF f16 =="
"$VENV" models/llama.cpp/convert_hf_to_gguf.py "$MERGED_DIR" \
    --outfile "$GGUF_F16" --outtype f16

echo "== AC5 step 4/6: quantize -> Q4_K_M =="
mkdir -p models/gguf
models/llama.cpp/build/bin/llama-quantize "$GGUF_F16" "$GGUF_Q4" Q4_K_M

echo "== AC5 step 5/6: register + load in LM Studio =="
mkdir -p "$LMS_DIR"
cp -v "$GGUF_Q4" "$LMS_DIR/"
if ! curl -sf http://localhost:1234/v1/models > /dev/null; then
    echo "Starting LM Studio server..."
    "$HOME/.lmstudio/bin/lms" server start
fi
# `lms load` keys on the model NAME as it appears in `lms ls` — NOT the
# publisher/path or the .gguf filename. A fresh copy also needs a rescan before
# the key resolves; `lms ls` triggers it. The model name is the LMS_DIR leaf.
MODEL_KEY="$(basename "$LMS_DIR")"
"$HOME/.lmstudio/bin/lms" ls > /dev/null 2>&1  # force rescan of the models dir
"$HOME/.lmstudio/bin/lms" load "$MODEL_KEY" -y --identifier ac5-qwen3-4b

# f16 GGUF is a disposable intermediate (~8G); the Q4_K_M is the kept artifact.
rm -f "$GGUF_F16"

echo "== AC5 step 6/6: validate tool-call parse rate (min=$MIN_RATE) =="
"$VENV" scripts/fine_tune/validate_tool_calls.py \
    --backend openai \
    --base-url http://localhost:1234/v1 \
    --model ac5-qwen3-4b \
    --min-parse-rate "$MIN_RATE"

echo "== AC5 COMPLETE: GGUF at $GGUF_Q4, validated against LM Studio =="
