#!/usr/bin/env bash
# Autonomous A/B sweep for v4.5 — runs 3 follow-on variants after the
# currently-running v4.5-a finishes, then merges + evals all four and
# writes a comparison report.
#
# Variants (each ~16-20h on this box):
#   v4.5-a (already running) — baseline knobs
#   v4.5-b — EARLY_STOP_PATIENCE=5 + EVAL_STEPS=250 (finer plateau detection)
#   v4.5-c — LORA_R=8  (lower rank — does smaller capacity generalize better?)
#   v4.5-d — LORA_R=32 (higher rank — does v4 underfit?)
#
# Hard-coded MAX_LENGTH=1024 across all variants — bumping to 2048 needs a
# token-length audit first, and on MPS roughly doubles per-step time.
#
# Usage:
#   nohup bash scripts/fine_tune/sweep_v4.5.sh > logs/m-ft-v4.5/sweep.log 2>&1 &
#
# Stop: kill the sweep PID (sweep.pid). Already-launched training runs
# continue independently; their PIDs are in logs/m-ft-v4.5/<stamp>.pid.

set -uo pipefail
# Not set -e: a single variant failing should not abort the rest of the sweep.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

SWEEP_LOG_DIR="$REPO_ROOT/logs/m-ft-v4.5"
mkdir -p "$SWEEP_LOG_DIR"
SWEEP_PID_FILE="$SWEEP_LOG_DIR/sweep.pid"
echo "$$" > "$SWEEP_PID_FILE"

# v4.5-a is already running. Discover its PID from the most recent full-*.pid.
A_PID_FILE="$(ls -t "$SWEEP_LOG_DIR"/full-*.pid 2>/dev/null | head -1)"
if [[ -z "${A_PID_FILE}" ]]; then
    echo "FAIL: no full-*.pid in $SWEEP_LOG_DIR — v4.5-a not launched?" >&2
    exit 1
fi
A_PID="$(cat "$A_PID_FILE")"
A_LOG="${A_PID_FILE%.pid}.log"

echo "================================================================"
echo "v4.5 autonomous sweep"
echo "================================================================"
echo "  sweep_pid:    $$"
echo "  variant a:    pid=$A_PID  log=$A_LOG"
echo "  variants:     b (patience=5 eval=250), c (r=8), d (r=32)"
echo "  start:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"

# --- Helpers ---------------------------------------------------------------

wait_for_pid_exit() {
    # Polls until the given PID is no longer alive. Sleep 60s between checks
    # (fine — runs are 16-20h).
    local pid="$1"
    local label="$2"
    echo "[$(date -u +%H:%M:%SZ)] waiting for $label (pid=$pid) to exit..."
    while kill -0 "$pid" 2>/dev/null; do
        sleep 60
    done
    echo "[$(date -u +%H:%M:%SZ)] $label exited."
}

run_variant() {
    # Launches a v4.5 variant with the env vars in $@ exported, then waits
    # for it to finish. Logs land in logs/m-ft-v4.5/<stamp>.log.
    local label="$1"; shift
    local extra_env=("$@")

    echo ""
    echo "================================================================"
    echo "[$(date -u +%H:%M:%SZ)] launching $label"
    for e in "${extra_env[@]}"; do echo "  $e"; done
    echo "================================================================"

    # Run launcher with extra env in a subshell — does not pollute the sweep
    # script's own env, so each variant starts fresh.
    (
        for e in "${extra_env[@]}"; do export "$e"; done
        export RUN_TAG="v4.5-${label}-full"
        bash "$REPO_ROOT/scripts/fine_tune/launch_v4.5.sh"
    ) &
    local launcher_pid=$!

    # The launcher itself backgrounds + waits, so launcher_pid lives until
    # the training python exits. Wait on it.
    wait "$launcher_pid"
    local rc=$?
    echo "[$(date -u +%H:%M:%SZ)] $label launcher exited rc=$rc"
    return $rc
}

# --- Wait for v4.5-a, then queue b/c/d ------------------------------------

wait_for_pid_exit "$A_PID" "v4.5-a"

# v4.5-b: finer plateau detection
run_variant "b" \
    "EARLY_STOP_PATIENCE=5" \
    "EVAL_STEPS=250" \
    "SAVE_STEPS=250"

# v4.5-c: lower rank
run_variant "c" \
    "LORA_R=8" \
    "LORA_ALPHA=16"

# v4.5-d: higher rank
run_variant "d" \
    "LORA_R=32" \
    "LORA_ALPHA=64"

# --- Post-sweep: merge GGUFs + eval ----------------------------------------

echo ""
echo "================================================================"
echo "[$(date -u +%H:%M:%SZ)] all variants done — building GGUFs"
echo "================================================================"

# Find the latest run dir for each variant. Variant a doesn't have a RUN_TAG
# suffix (it was launched before the sweep); b/c/d do.
ROOT="$REPO_ROOT/models/lora/qwen3-4b-toolcalls-lora/runs"

find_latest() {
    local pattern="$1"
    ls -td "$ROOT"/*${pattern}* 2>/dev/null | head -1
}

A_DIR="$(find_latest v4.5-full)"
B_DIR="$(find_latest v4.5-b-full)"
C_DIR="$(find_latest v4.5-c-full)"
D_DIR="$(find_latest v4.5-d-full)"

for v in a b c d; do
    var="${v^^}_DIR"
    dir="${!var}"
    if [[ -z "$dir" || ! -d "$dir" ]]; then
        echo "WARN: v4.5-$v dir not found (var=$var) — skipping merge"
        continue
    fi
    echo "v4.5-$v dir: $dir"
    "$REPO_ROOT/.venv-finetune/bin/python" \
        "$REPO_ROOT/scripts/fine_tune/merge_checkpoint.py" "$dir" \
        > "$SWEEP_LOG_DIR/merge-v4.5-$v.log" 2>&1
    echo "  merge rc=$?"
done

echo ""
echo "================================================================"
echo "[$(date -u +%H:%M:%SZ)] running eval_harder across variants"
echo "================================================================"

# eval_harder.py serves one model at a time — call it per variant and
# capture per-variant JSON outputs, then summarize.
EVAL_OUT_DIR="$SWEEP_LOG_DIR/eval-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVAL_OUT_DIR"
for v in a b c d; do
    gguf="$(ls -t "$REPO_ROOT/models/lora/qwen3-4b-toolcalls-lora/runs"/*v4.5*${v}*/merged/*Q6_K*.gguf 2>/dev/null | head -1)"
    if [[ -z "$gguf" ]]; then
        echo "WARN: no Q6_K GGUF for v4.5-$v — skipping eval"
        continue
    fi
    echo "evaluating v4.5-$v gguf=$gguf"
    "$REPO_ROOT/.venv-finetune/bin/python" \
        "$REPO_ROOT/scripts/fine_tune/eval_harder.py" \
        --model "v4.5-$v=$gguf" \
        > "$EVAL_OUT_DIR/v4.5-$v.txt" 2>&1
done

# Also rerun v4 as the comparison baseline (it's on disk already).
V4_GGUF="$(ls -t "$REPO_ROOT/models/lora/qwen3-4b-toolcalls-lora/runs"/*v4-full/merged/*Q6_K*.gguf 2>/dev/null | head -1)"
if [[ -n "$V4_GGUF" ]]; then
    echo "evaluating v4 baseline gguf=$V4_GGUF"
    "$REPO_ROOT/.venv-finetune/bin/python" \
        "$REPO_ROOT/scripts/fine_tune/eval_harder.py" \
        --model "v4=$V4_GGUF" \
        > "$EVAL_OUT_DIR/v4.txt" 2>&1
fi

# Write summary
SUMMARY="$EVAL_OUT_DIR/SWEEP_RESULTS.md"
{
    echo "# v4.5 sweep results"
    echo ""
    echo "Sweep finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo ""
    echo "## Per-variant eval_harder output"
    echo ""
    for v in a b c d; do
        echo "### v4.5-$v"
        echo '```'
        tail -30 "$EVAL_OUT_DIR/v4.5-$v.txt" 2>/dev/null || echo "(no output)"
        echo '```'
        echo ""
    done
    echo "### v4 (baseline)"
    echo '```'
    tail -30 "$EVAL_OUT_DIR/v4.txt" 2>/dev/null || echo "(no output)"
    echo '```'
} > "$SUMMARY"

echo ""
echo "================================================================"
echo "[$(date -u +%H:%M:%SZ)] SWEEP COMPLETE"
echo "  summary: $SUMMARY"
echo "  evals:   $EVAL_OUT_DIR/"
echo "================================================================"

# Best-effort notification (silently fails if osascript unavailable)
osascript -e 'display notification "v4.5 sweep complete — see SWEEP_RESULTS.md" with title "agentMemory"' 2>/dev/null || true

rm -f "$SWEEP_PID_FILE"
