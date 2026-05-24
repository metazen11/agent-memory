#!/usr/bin/env bash
# v5 pilot launcher — Qwen2.5-3B LoRA on the v5 dataset (Anvil prompt +
# project features + path normalization).
#
# Usage:
#   bash scripts/fine_tune/launch_v5_pilot.sh smoke     # 50 rows, ~5min
#   bash scripts/fine_tune/launch_v5_pilot.sh full      # 4500 rows, ~3-6h
#   nohup bash scripts/fine_tune/launch_v5_pilot.sh full > /dev/null 2>&1 &
#
# Defaults to 'smoke' if no arg passed.

set -euo pipefail

MODE="${1:-smoke}"
if [[ "$MODE" != "smoke" && "$MODE" != "full" ]]; then
    echo "FAIL: mode must be 'smoke' or 'full', got '$MODE'" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Sanity guards
if [[ -L models || -L .venv-finetune ]]; then
    echo "FAIL: models/ or .venv-finetune/ is a symlink. Use local SSD." >&2
    exit 1
fi
if readlink -f models 2>/dev/null | grep -qi dropbox; then
    echo "FAIL: models/ resolves into Dropbox." >&2
    exit 1
fi

# v5 dataset + base — switched to Qwen3-4B for 128k+ context (256k native)
export MODEL_SLUG="qwen3-4b"
export DATASET_VERSION="v5-pilot"
export DATASET_FAMILY="qwen3_tools"

if [[ "$MODE" == "smoke" ]]; then
    export DATASET_TIER="tiny"
    export RUN_TAG="v5-pilot-smoke"
    export EPOCHS="${EPOCHS:-0.5}"
    # 4096 chosen — fits long agent transcripts; Qwen3-4B native 256k context
    # at inference so we are far from the architectural limit.
    export MAX_LENGTH="${MAX_LENGTH:-4096}"
    export EVAL_STEPS="${EVAL_STEPS:-25}"
    export SAVE_STEPS="${SAVE_STEPS:-50}"
    export LOGGING_STEPS="${LOGGING_STEPS:-5}"
else
    export DATASET_TIER="full"
    export RUN_TAG="v5-pilot-full"
    export EPOCHS="${EPOCHS:-1.0}"
    # 2048 — proven safe at Qwen3-4B. 3072 caused unified-memory swap
    # thrashing (every step paged from disk, step time ballooned 30s→290s).
    # 2048 + grad_accum=2 keeps RSS within unified memory budget.
    # If MAX_LENGTH=3072 is desired later, also set GRAD_ACCUM=1.
    export MAX_LENGTH="${MAX_LENGTH:-2048}"
    export GRAD_ACCUM="${GRAD_ACCUM:-2}"
    # PROVEN_PRACTICES knobs
    export LR_SCHEDULER_TYPE="${LR_SCHEDULER_TYPE:-cosine}"
    export WARMUP_RATIO="${WARMUP_RATIO:-0.05}"
    export WEIGHT_DECAY="${WEIGHT_DECAY:-0.01}"
    export EARLY_STOP_PATIENCE="${EARLY_STOP_PATIENCE:-3}"
    export LOAD_BEST_AT_END="${LOAD_BEST_AT_END:-1}"
    export EVAL_STEPS="${EVAL_STEPS:-250}"
    export SAVE_STEPS="${SAVE_STEPS:-250}"   # multiple of eval_steps for load_best
fi

# Output paths
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$REPO_ROOT/logs/m-ft-v5-pilot"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${MODE}-${STAMP}.log"
PID_FILE="$LOG_DIR/${MODE}-${STAMP}.pid"

cat <<EOF
================================================================
v5 PILOT launch
================================================================
  mode:         ${MODE}
  stamp:        ${STAMP}
  log:          ${LOG}
  pid_file:     ${PID_FILE}
  model:        ${MODEL_SLUG} (${DATASET_FAMILY}/${DATASET_VERSION}, ${DATASET_TIER})
  epochs:       ${EPOCHS}
  max_length:   ${MAX_LENGTH}
  eval/save:    ${EVAL_STEPS}/${SAVE_STEPS}
  python:       ${REPO_ROOT}/.venv-finetune/bin/python
  caffeinate:   yes
================================================================
EOF

# Use the qwen2.5-3b trainer (knows how to handle the chat format)
TRAINER="$REPO_ROOT/models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py"
if [[ ! -f "$TRAINER" ]]; then
    echo "FAIL: trainer not found at $TRAINER" >&2
    exit 1
fi

caffeinate -di "$REPO_ROOT/.venv-finetune/bin/python" -u \
    "$TRAINER" > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"
echo "launched pid=${PID}"
echo "  tail -f ${LOG}"
wait "$PID"
RC=$?
echo "trainer exited rc=${RC}"
exit "$RC"
