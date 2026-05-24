#!/usr/bin/env bash
# Launch the v4.5 Qwen3-4B LoRA training run with PROVEN_PRACTICES knobs.
#
# Differs from launch_v3.sh in the env-var overrides only — the trainer
# script + caffeinate wrapper + heartbeat + NanGuard are reused.
#
# PROVEN_PRACTICES.md top-5 knobs:
#   1. dedup + OOD eval (data-side, handled in build_v4_5_dataset.py)
#   2. MAX_LENGTH=1024 stays (DEFER bump to v4.6 — needs token-length audit)
#   3. warmup_ratio=0.05 + cosine LR
#   4. EarlyStoppingCallback(patience=3) + load_best_model_at_end=True
#      + weight_decay=0.01
#   5. eval cap stays at full valid set for now (we want strong eval signal)
#
# Usage:
#   bash scripts/fine_tune/launch_v4.5.sh           # foreground
#   nohup bash scripts/fine_tune/launch_v4.5.sh &   # background

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Sanity: same hard checks as launch_v3.sh
if [[ -L models || -L .venv-finetune ]]; then
    echo "FAIL: models/ or .venv-finetune/ is a symlink. Move to local SSD first." >&2
    exit 1
fi
if readlink -f models | grep -qi dropbox; then
    echo "FAIL: models/ resolves into Dropbox. Aborting." >&2
    exit 1
fi

# v4.5 dataset + base
export MODEL_SLUG="${MODEL_SLUG:-qwen3-4b}"
export DATASET_TIER="${DATASET_TIER:-full}"
export DATASET_VERSION="${DATASET_VERSION:-v4.5}"
export DATASET_FAMILY="${DATASET_FAMILY:-qwen3_tools}"
export RUN_TAG="${RUN_TAG:-v4.5-full}"

# v4.5 PROVEN_PRACTICES knobs
export LR_SCHEDULER_TYPE="${LR_SCHEDULER_TYPE:-cosine}"
export WARMUP_RATIO="${WARMUP_RATIO:-0.05}"
export WEIGHT_DECAY="${WEIGHT_DECAY:-0.01}"
export EARLY_STOP_PATIENCE="${EARLY_STOP_PATIENCE:-3}"
export LOAD_BEST_AT_END="${LOAD_BEST_AT_END:-1}"

# load_best_model_at_end requires save_steps to be a multiple of eval_steps so
# the best eval checkpoint is always on disk. Default trainer config has
# save=250, eval=500 (save more often to survive crashes) which trips
# validation. Align save=eval=500 — the persisted "best" is whatever eval
# picks. Lose 250-step crash-recovery granularity, gain load_best semantics.
export EVAL_STEPS="${EVAL_STEPS:-500}"
export SAVE_STEPS="${SAVE_STEPS:-500}"

# Output paths (mirror launch_v3.sh)
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$REPO_ROOT/logs/m-ft-v4.5"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${DATASET_TIER}-${STAMP}.log"
PID_FILE="$LOG_DIR/${DATASET_TIER}-${STAMP}.pid"

cat <<EOF
================================================================
v4.5 LoRA launch wrapper
================================================================
  stamp:        ${STAMP}
  log:          ${LOG}
  pid_file:     ${PID_FILE}
  model:        ${MODEL_SLUG} (${DATASET_FAMILY}/${DATASET_VERSION}, ${DATASET_TIER})
  knobs:        lr=${LR_SCHEDULER_TYPE} warmup=${WARMUP_RATIO} wd=${WEIGHT_DECAY}
                early_stop=patience=${EARLY_STOP_PATIENCE} load_best=${LOAD_BEST_AT_END}
  python:       ${REPO_ROOT}/.venv-finetune/bin/python
  caffeinate:   yes (-di, no sleep / no display sleep)
================================================================
EOF

caffeinate -di "$REPO_ROOT/.venv-finetune/bin/python" -u \
    "$REPO_ROOT/models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py" \
    > "$LOG" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"
echo "launched pid=${PID}, tailing log:"
echo "  tail -f ${LOG}"
wait "$PID"
