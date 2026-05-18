#!/usr/bin/env bash
# Launch the v3 Qwen3-4B LoRA tool-call training run on local SSD with safety wrappers.
#
# Wraps run_train_lora.py with:
#   - caffeinate -di: prevents system sleep + display sleep for the duration
#     (prior v3 run died after log freeze followed by reboot at 00:26 UTC)
#   - timestamped log dir under logs/m-ft-v3/
#   - records PID so it can be tracked / killed cleanly
#
# Trainer itself (run_train_lora.py) has Fp32EvalTrainer + NanGuardCallback
# patches that fail-fast on NaN eval_loss instead of burning 13h.
#
# Usage:
#   bash scripts/fine_tune/launch_v3.sh           # foreground
#   nohup bash scripts/fine_tune/launch_v3.sh &   # background

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# --- Refuse to launch if anything still lives in Dropbox -------------------
if [[ -L models || -L .venv-finetune ]]; then
    echo "FAIL: models/ or .venv-finetune/ is still a symlink. Move them to local SSD before launching." >&2
    readlink models .venv-finetune 2>/dev/null || true
    exit 1
fi
if readlink -f models | grep -qi dropbox; then
    echo "FAIL: models/ resolves into Dropbox. Aborting to prevent sync-induced kills." >&2
    exit 1
fi

# --- Env for the trainer ---------------------------------------------------
export MODEL_SLUG="${MODEL_SLUG:-qwen3-4b}"
export DATASET_TIER="${DATASET_TIER:-full}"
export DATASET_VERSION="${DATASET_VERSION:-v3}"
export DATASET_FAMILY="${DATASET_FAMILY:-qwen3_tools}"
export RUN_TAG="${RUN_TAG:-v3-full}"

# --- Output paths ----------------------------------------------------------
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="$REPO_ROOT/logs/m-ft-v3"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/full-${STAMP}.log"
PID_FILE="$LOG_DIR/full-${STAMP}.pid"

# --- Preflight banner ------------------------------------------------------
{
    echo "================================================================"
    echo "v3 LoRA launch wrapper"
    echo "================================================================"
    echo "  stamp:     $STAMP"
    echo "  log:       $LOG_FILE"
    echo "  pid_file:  $PID_FILE"
    echo "  model:     $MODEL_SLUG ($DATASET_FAMILY/$DATASET_VERSION, $DATASET_TIER)"
    echo "  python:    $(.venv-finetune/bin/python -c 'import sys; print(sys.executable)')"
    echo "  torch:     $(.venv-finetune/bin/python -c 'import torch; print(torch.__version__)')"
    echo "  models/:   $(readlink -f models)"
    echo "  venv:      $(readlink -f .venv-finetune)"
    echo "  caffeinate: yes (-di, no sleep / no display sleep)"
    echo "================================================================"
} | tee -a "$LOG_FILE"

# --- Launch under caffeinate so the system can't sleep -----------------------
exec caffeinate -di .venv-finetune/bin/python \
    models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py \
    >> "$LOG_FILE" 2>&1 &

CHILD=$!
echo "$CHILD" > "$PID_FILE"
echo "launched pid=$CHILD, tailing log:" | tee -a "$LOG_FILE"
echo "  tail -f $LOG_FILE"

wait "$CHILD"
RC=$?
echo "exit_code=$RC" | tee -a "$LOG_FILE"
exit "$RC"
