#!/usr/bin/env bash
# launch_pilot_4b.sh — one-command wrapper for the Qwen3-4B v5 PILOT on HF Jobs.
#
# This is the "trigger-and-reboot" entry point. It pins the exact pilot
# parameters (private WFCA org repos, l4x1, 4h timeout, 4096 max_length) and
# delegates to run_hf_job.sh, which is DRY-RUN BY DEFAULT. Nothing costs money
# unless you pass --launch.
#
#   Dry-run (default — prints the exact command, spends nothing):
#     bash scripts/fine_tune/launch_pilot_4b.sh
#
#   Actually submit the PAID job (requires human approval):
#     export HF_TOKEN=$(hf auth token)
#     bash scripts/fine_tune/launch_pilot_4b.sh --launch
#
# After launch you can REBOOT your Mac — the job runs on HF's GPU independently.
# Come back and check status with the commands printed at the end (also in
# docs/runbooks/pilot-4b-reboot-checklist.md).
# ---------------------------------------------------------------------------

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# --- PILOT parameters (private WFCA org repos; verified private) ------------
export BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B}"
export DATASET="${DATASET:-WFCA/agentmem-toolcalls-v5-pilot}"          # PRIVATE
export HUB_MODEL_ID="${HUB_MODEL_ID:-WFCA/qwen3-4b-toolcalls-v5-pilot}" # PRIVATE
export PUSH_PRIVATE="${PUSH_PRIVATE:-1}"                                # adapter pushed PRIVATE
export HF_NAMESPACE="${HF_NAMESPACE:-WFCA}"
export RUN_TAG="${RUN_TAG:-v5-pilot}"

export HW_FLAVOR="${HW_FLAVOR:-l4x1}"        # 1x L4 24GB, ~$0.80/hr
# MEASURED (run 6a4d815, 2026-07-07): 1 epoch / 5k rows / l4x1 = ~2h59m training
# + eval + push ≈ 3h12m wall. The old 2h ceiling was too short and the job was
# killed 'Job timeout' AFTER a successful adapter push (status ERROR, artifact OK).
# 4h gives headroom so the job terminates SUCCESS. (Consider a10g to cut runtime.)
export JOB_TIMEOUT="${JOB_TIMEOUT:-4h}"      # safety ceiling; measured run ~3h12m
export MAX_LENGTH="${MAX_LENGTH:-4096}"      # Mac 2048 cap does NOT apply on VRAM
export EPOCHS="${EPOCHS:-1.0}"
export LR="${LR:-2e-4}"
export PER_DEVICE_BATCH="${PER_DEVICE_BATCH:-2}"
export GRAD_ACCUM="${GRAD_ACCUM:-4}"
export LORA_R="${LORA_R:-16}"
export LORA_ALPHA="${LORA_ALPHA:-32}"
export LORA_DROPOUT="${LORA_DROPOUT:-0.05}"

# Local training script — `hf jobs uv run` uploads it automatically.
export TRAIN_SCRIPT="${TRAIN_SCRIPT:-scripts/fine_tune/train_hf_job.py}"

echo "############################################################"
echo "# Qwen3-4B v5 PILOT — HF Jobs (private WFCA org)"
echo "#   dataset:   $DATASET   (private)"
echo "#   output:    $HUB_MODEL_ID   (private)"
echo "#   flavor:    $HW_FLAVOR   timeout: $JOB_TIMEOUT"
echo "############################################################"

# Delegate to the guarded launcher (dry-run by default; passes through --launch).
bash scripts/fine_tune/run_hf_job.sh "$@"

# --- Post-reboot status commands (printed on dry-run AND after launch) ------
cat <<'REBOOT'

------------------------------------------------------------------
 COME BACK TO IT — post-reboot status commands
------------------------------------------------------------------
 After launching you can reboot your Mac. The job runs on HF's GPU.
 Find + watch the job:

   hf jobs ps                                 # running jobs (alias for list --status RUNNING)
   hf jobs list --label agentmem-finetune     # all agentmem jobs + IDs
   JOB_ID=<paste the id>
   hf jobs inspect "$JOB_ID"                   # status / flavor / timing
   hf jobs logs "$JOB_ID" --follow             # stream logs (loss should trend down)

 DONE when the adapter appears at the PRIVATE model repo:
   hf download WFCA/qwen3-4b-toolcalls-v5-pilot --local-dir models/lora/_hf_pull/v5-pilot
   # (adapter_model.safetensors present == training finished + pushed)

 Then get the local GGUF (see docs/runbooks/pilot-4b-reboot-checklist.md).
------------------------------------------------------------------
REBOOT
