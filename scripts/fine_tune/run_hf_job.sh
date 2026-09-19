#!/usr/bin/env bash
# run_hf_job.sh — thin, parametrized wrapper around `hf jobs uv run` for the
# agent-memory LoRA fine-tune on Hugging Face Jobs (rented GPU).
#
# Style mirrors launch_v5_pilot.sh: every tunable is an env var with a
# documented default. NOTHING costs money by accident — the wrapper is
# DRY-RUN BY DEFAULT and only constructs + PRINTS the command. Pass --launch
# to actually submit the job.
#
# Companion runbook: docs/runbooks/hf-jobs-finetune.md (parameter table +
# phase gates). This script implements Phase 2 (launch) of that runbook.
#
# ---------------------------------------------------------------------------
# USAGE
# ---------------------------------------------------------------------------
#   # Dry-run (default) — prints the exact `hf jobs uv run` command, runs guards:
#   bash scripts/fine_tune/run_hf_job.sh
#
#   # 4B pilot, explicit params, still dry-run:
#   BASE_MODEL=Qwen/Qwen3-4B DATASET=WFCAMZ/agentmem-v5-pilot HW_FLAVOR=l4x1 \
#     bash scripts/fine_tune/run_hf_job.sh
#
#   # Actually submit (spends money):
#   BASE_MODEL=Qwen/Qwen3-8B DATASET=WFCAMZ/agentmem-v5-pilot HW_FLAVOR=a100-large \
#     bash scripts/fine_tune/run_hf_job.sh --launch
#
# The wrapper does NOT itself convert to GGUF or push GGUF — the UV training
# script it invokes pushes the LoRA adapter (+ optional merged model) to the
# Hub. GGUF conversion is a later runbook phase (see the runbook).
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# PARAMETERS (env-var driven — see docs/runbooks/hf-jobs-finetune.md)
# ---------------------------------------------------------------------------

# --- What to train ---
BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B}"          # HF repo id of the base model
# DATASET is a PRIVATE Hub repo — the training data is sensitive/internal and
# MUST never be public. Default is the WFCA org private pilot dataset.
DATASET="${DATASET:-WFCA/agentmem-toolcalls-v5-pilot}"  # HF dataset repo id (PRIVATE, on Hub)
DATASET_SPLIT="${DATASET_SPLIT:-train}"            # split to train on
DATASET_CONFIG="${DATASET_CONFIG:-}"               # optional dataset config name

# --- Where results go (Hub) ---
# Namespace: WFCA (org, paying — default) or WFCAMZ (personal). `hf auth whoami`
# shows both. Outputs are PRIVATE by policy (PUSH_PRIVATE=1).
HF_NAMESPACE="${HF_NAMESPACE:-WFCA}"
RUN_TAG="${RUN_TAG:-v5-pilot}"                      # short tag; becomes part of the model id
# Derived output model id unless caller overrides HUB_MODEL_ID directly.
_MODEL_SLUG="$(printf '%s' "$BASE_MODEL" | tr '/' '-' | tr '[:upper:]' '[:lower:]')"
HUB_MODEL_ID="${HUB_MODEL_ID:-${HF_NAMESPACE}/agentmem-${_MODEL_SLUG}-${RUN_TAG}}"
PUSH_PRIVATE="${PUSH_PRIVATE:-1}"                  # 1 = push the adapter PRIVATE (default; data governance)
PUSH_MERGED="${PUSH_MERGED:-0}"                    # 1 = also push the merged (base+LoRA) model

# --- Hardware (verify live: `hf jobs hardware`) ---
# Common flavors + list price (2026-07-04): l4x1 $0.80/hr, a10g-small $1.00/hr,
# a10g-large, a100-large $2.50/hr, h200 $5.00/hr. Full valid set below.
HW_FLAVOR="${HW_FLAVOR:-l4x1}"
JOB_TIMEOUT="${JOB_TIMEOUT:-4h}"                   # MUST exceed training time. MEASURED: 1 epoch/5k rows/l4x1 ≈ 3h12m (run 6a4d815); 2h was too short → killed 'Job timeout' post-push (ERROR status, artifact OK).

# --- Training hyperparameters ---
# NOTE (Mac-local vs HF-Jobs): the MAX_LENGTH=2048 / GRAD_ACCUM=2 ceiling in
# launch_v5_pilot.sh existed ONLY because of Mac unified-memory swap thrash.
# On a real VRAM budget (L4/A10G 24GB, A100 80GB) that constraint does NOT
# apply — raise MAX_LENGTH to cover full transcripts. See the runbook's
# "Mac-local vs HF-Jobs" column.
MAX_LENGTH="${MAX_LENGTH:-4096}"
EPOCHS="${EPOCHS:-1.0}"
LR="${LR:-2e-4}"
LR_SCHEDULER_TYPE="${LR_SCHEDULER_TYPE:-cosine}"
WARMUP_RATIO="${WARMUP_RATIO:-0.05}"
WEIGHT_DECAY="${WEIGHT_DECAY:-0.01}"
PER_DEVICE_BATCH="${PER_DEVICE_BATCH:-2}"
GRAD_ACCUM="${GRAD_ACCUM:-4}"
EVAL_STEPS="${EVAL_STEPS:-50}"
SAVE_STEPS="${SAVE_STEPS:-50}"
LOGGING_STEPS="${LOGGING_STEPS:-5}"
VALID_SPLIT_PCT="${VALID_SPLIT_PCT:-0.05}"         # held-out fraction for eval

# --- LoRA ---
LORA_R="${LORA_R:-16}"
LORA_ALPHA="${LORA_ALPHA:-32}"
LORA_DROPOUT="${LORA_DROPOUT:-0.05}"

# --- Monitoring (Trackio) ---
TRACKIO_PROJECT="${TRACKIO_PROJECT:-agentmem-finetune}"
TRACKIO_RUN="${TRACKIO_RUN:-${RUN_TAG}-${_MODEL_SLUG}}"

# --- Base container image for the UV job ---
JOB_IMAGE="${JOB_IMAGE:-}"                          # empty = let `hf jobs uv run` pick its default

# --- The training script (LOCAL FILE or URL) ---
# VERIFIED (2026-07-04): `hf jobs uv run SCRIPT` accepts a LOCAL FILE PATH and
# uploads it to the ephemeral container automatically — it does NOT need to be
# pre-published to the Hub. A URL also works. So TRAIN_SCRIPT can be:
#   - a local path like scripts/fine_tune/train_hf_job.py (default; uploaded), OR
#   - a URL (https://.../train_hf_job.py).
# The PEP723 inline UV deps in train_hf_job.py are resolved by uv in-container.
TRAIN_SCRIPT="${TRAIN_SCRIPT:-scripts/fine_tune/train_hf_job.py}"
# Back-compat: if an old caller sets TRAIN_SCRIPT_URL, honor it.
TRAIN_SCRIPT="${TRAIN_SCRIPT_URL:-$TRAIN_SCRIPT}"

# ---------------------------------------------------------------------------
# VALID FLAVORS (from `hf jobs run --flavor` enum, 2026-07-04)
# ---------------------------------------------------------------------------
VALID_FLAVORS="cpu-basic cpu-upgrade cpu-performance cpu-xl t4-small t4-medium \
l4x1 l4x4 l40sx1 l40sx4 l40sx8 a10g-small a10g-large a10g-largex2 a10g-largex4 \
a100-large a100x4 a100x8 h200 h200x2 h200x4 h200x8 rtx-pro-6000 rtx-pro-6000x2 \
rtx-pro-6000x4 rtx-pro-6000x8"

# ---------------------------------------------------------------------------
# SANITY GUARDS (fail fast BEFORE printing/launching)
# ---------------------------------------------------------------------------
fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. hf CLI present.
command -v hf >/dev/null 2>&1 || fail "hf CLI not found on PATH. Install: curl -LsSf https://hf.co/cli/install.sh | bash"

# 2. Authenticated (count-only — never echo the token).
if ! hf auth whoami >/dev/null 2>&1; then
    fail "hf not authenticated. Run: hf auth login  (need a WRITE token for Hub push)."
fi
WHO="$(hf auth whoami 2>/dev/null | head -1)"
echo "[auth] $WHO"

# 3. HF_TOKEN must be exported so we can pass it to the ephemeral job as a
#    secret. Without it, push_to_hub fails and the whole run is lost.
#    Count-only check — do NOT print the value.
if [ -z "${HF_TOKEN:-}" ]; then
    echo "WARN: HF_TOKEN is not exported in this shell." >&2
    echo "      The job needs it as a secret (passed via bare '--secrets HF_TOKEN')" >&2
    echo "      to push results from the ephemeral container. Export before --launch:" >&2
    echo "        export HF_TOKEN=\$(hf auth token)" >&2
    TOKEN_PRESENT=0
else
    # Confirm presence + rough shape only (count chars, never echo).
    _tok_len="${#HF_TOKEN}"
    [ "$_tok_len" -ge 20 ] || fail "HF_TOKEN is set but looks too short (${_tok_len} chars)."
    echo "[auth] HF_TOKEN present (length ${_tok_len})."
    TOKEN_PRESENT=1
fi

# 4. Flavor must be in the valid set.
_flavor_ok=0
for f in $VALID_FLAVORS; do [ "$f" = "$HW_FLAVOR" ] && _flavor_ok=1 && break; done
[ "$_flavor_ok" = 1 ] || fail "HW_FLAVOR='$HW_FLAVOR' not in valid set. Run: hf jobs hardware. Valid: $VALID_FLAVORS"

# 5. Dataset must look like a Hub repo id (namespace/name), not a local path.
case "$DATASET" in
    */*) : ;;  # ok, has a namespace
    *) fail "DATASET='$DATASET' does not look like a Hub repo id (expected 'namespace/name'). Jobs cannot read local files; push the dataset to the Hub first (see runbook Phase 1)." ;;
esac
if [ -e "$DATASET" ]; then
    fail "DATASET='$DATASET' resolves to a LOCAL path. Jobs run in an isolated container — push the dataset to the Hub and pass its repo id instead."
fi

# 6. Confirm the dataset exists on the Hub (best-effort; warn if the probe fails).
if hf datasets info "$DATASET" >/dev/null 2>&1; then
    echo "[dataset] $DATASET exists on the Hub."
else
    echo "WARN: could not confirm dataset '$DATASET' on the Hub (hf datasets info failed)." >&2
    echo "      Verify it is pushed + you have read access before --launch." >&2
fi

# 7. Training script must be set. It may be a LOCAL FILE (uploaded automatically
#    by `hf jobs uv run`) or a URL. If it's a local path, it must exist.
if [ -z "$TRAIN_SCRIPT" ]; then
    fail "TRAIN_SCRIPT is empty. Set it to scripts/fine_tune/train_hf_job.py (local, auto-uploaded) or a URL."
fi
case "$TRAIN_SCRIPT" in
    http://*|https://*)
        echo "[script] TRAIN_SCRIPT is a URL: $TRAIN_SCRIPT" ;;
    *)
        [ -f "$TRAIN_SCRIPT" ] || fail "TRAIN_SCRIPT='$TRAIN_SCRIPT' is a local path but does not exist (cwd=$REPO_ROOT)."
        echo "[script] TRAIN_SCRIPT is a local file (auto-uploaded by 'hf jobs uv run'): $TRAIN_SCRIPT" ;;
esac

# ---------------------------------------------------------------------------
# CONSTRUCT the `hf jobs uv run` command.
# ---------------------------------------------------------------------------
# All training knobs are passed to the UV script as --env so the script (which
# lives on the Hub, REQUIRES-REVIEW) reads them from os.environ. This keeps the
# wrapper agnostic to the script's internals while staying fully parametrized.

# Build the --env argument list. (Secrets are passed via --secrets, NOT --env.)
ENV_ARGS=(
    --env "BASE_MODEL=${BASE_MODEL}"
    --env "DATASET=${DATASET}"
    --env "DATASET_SPLIT=${DATASET_SPLIT}"
    --env "DATASET_CONFIG=${DATASET_CONFIG}"
    --env "HUB_MODEL_ID=${HUB_MODEL_ID}"
    --env "PUSH_PRIVATE=${PUSH_PRIVATE}"
    --env "PUSH_MERGED=${PUSH_MERGED}"
    --env "MAX_LENGTH=${MAX_LENGTH}"
    --env "EPOCHS=${EPOCHS}"
    --env "LR=${LR}"
    --env "LR_SCHEDULER_TYPE=${LR_SCHEDULER_TYPE}"
    --env "WARMUP_RATIO=${WARMUP_RATIO}"
    --env "WEIGHT_DECAY=${WEIGHT_DECAY}"
    --env "PER_DEVICE_BATCH=${PER_DEVICE_BATCH}"
    --env "GRAD_ACCUM=${GRAD_ACCUM}"
    --env "EVAL_STEPS=${EVAL_STEPS}"
    --env "SAVE_STEPS=${SAVE_STEPS}"
    --env "LOGGING_STEPS=${LOGGING_STEPS}"
    --env "VALID_SPLIT_PCT=${VALID_SPLIT_PCT}"
    --env "LORA_R=${LORA_R}"
    --env "LORA_ALPHA=${LORA_ALPHA}"
    --env "LORA_DROPOUT=${LORA_DROPOUT}"
    --env "TRACKIO_PROJECT=${TRACKIO_PROJECT}"
    --env "TRACKIO_RUN=${TRACKIO_RUN}"
)

CMD=(hf jobs uv run --flavor "$HW_FLAVOR" --timeout "$JOB_TIMEOUT")
# JOB_NAMESPACE routes billing + execution to an org (e.g. WFCA) instead of the
# running user's personal namespace. Personal WFCAMZ has no Jobs credits (402);
# set JOB_NAMESPACE=WFCA to bill the org. Empty = user's own namespace.
[ -n "${JOB_NAMESPACE:-}" ] && CMD+=(--namespace "$JOB_NAMESPACE")
[ -n "$JOB_IMAGE" ] && CMD+=(--image "$JOB_IMAGE")
CMD+=("${ENV_ARGS[@]}")
# Secret: the bare `--secrets HF_TOKEN` form tells hf to inject the value from
# the caller's HF_TOKEN env var (or the token file if unset) into the ephemeral
# container. This is the documented, robust form — no shell/CLI re-expansion of
# a `$VAR` literal, which could otherwise pass the literal string and fail the
# private dataset load + final push AFTER the full GPU spend (review H1, #55).
CMD+=(--secrets HF_TOKEN)
# HF job labels are flat single-token keys — a colon (`run:tag`) is rejected as
# an invalid key. Use a hyphen so the label is valid and still filterable.
CMD+=(--label "agentmem-finetune" --label "run-${RUN_TAG}")
# Script last (positional SCRIPT arg). Local file OR URL — `hf jobs uv run`
# uploads a local file automatically.
CMD+=("${TRAIN_SCRIPT:-<SET_TRAIN_SCRIPT>}")

# ---------------------------------------------------------------------------
# Rough cost estimate (list price × timeout hours upper bound).
# ---------------------------------------------------------------------------
case "$HW_FLAVOR" in
    l4x1)        RATE=0.80 ;;
    a10g-small)  RATE=1.00 ;;
    a10g-large)  RATE=1.50 ;;
    a100-large)  RATE=2.50 ;;
    h200)        RATE=5.00 ;;
    t4-small)    RATE=0.40 ;;
    *)           RATE="" ;;
esac

# Convert JOB_TIMEOUT (e.g. "2h", "90m", "1h30m" is NOT supported — use h OR m)
# to a decimal number of hours. Empty/unparseable -> "" (skip cost line).
_timeout_hours() {
    local t="$1"
    case "$t" in
        *[hH]) awk -v v="${t%[hH]}" 'BEGIN{ if (v+0>0) printf "%.4f", v; }' ;;
        *[mM]) awk -v v="${t%[mM]}" 'BEGIN{ if (v+0>0) printf "%.4f", v/60; }' ;;
        *)     awk -v v="$t"        'BEGIN{ if (v+0>0) printf "%.4f", v; }' ;;
    esac
}
_TIMEOUT_H="$(_timeout_hours "$JOB_TIMEOUT")"

# ---------------------------------------------------------------------------
# PRINT the plan.
# ---------------------------------------------------------------------------
cat <<EOF

================================================================
 HF JOBS FINE-TUNE — PLAN
================================================================
  base_model:     ${BASE_MODEL}
  dataset:        ${DATASET} (split=${DATASET_SPLIT}${DATASET_CONFIG:+, config=${DATASET_CONFIG}})
  hub_model_id:   ${HUB_MODEL_ID}
  push_private:   ${PUSH_PRIVATE}   (1 = adapter pushed PRIVATE — data governance)
  push_merged:    ${PUSH_MERGED}
  hw_flavor:      ${HW_FLAVOR}$([ -n "$RATE" ] && echo "  (~\$${RATE}/hr list)")
  timeout:        ${JOB_TIMEOUT}
  max_length:     ${MAX_LENGTH}   (Mac ceiling of 2048 does NOT apply on VRAM)
  epochs:         ${EPOCHS}   lr=${LR} sched=${LR_SCHEDULER_TYPE} warmup=${WARMUP_RATIO}
  batch/accum:    ${PER_DEVICE_BATCH} x ${GRAD_ACCUM}
  lora:           r=${LORA_R} alpha=${LORA_ALPHA} dropout=${LORA_DROPOUT}
  eval/save:      ${EVAL_STEPS}/${SAVE_STEPS}   valid_split=${VALID_SPLIT_PCT}
  trackio:        ${TRACKIO_PROJECT} / ${TRACKIO_RUN}
  train_script:   ${TRAIN_SCRIPT:-<UNSET>}
EOF
if [ -n "$RATE" ] && [ -n "$_TIMEOUT_H" ]; then
    _COST="$(awk -v r="$RATE" -v h="$_TIMEOUT_H" 'BEGIN{printf "%.2f", r*h}')"
    echo "  cost ceiling:   ~\$${_COST} (rate x timeout; real run is usually shorter)"
fi
cat <<EOF
================================================================

Command that WOULD be submitted:

EOF
# Print the command, one token per line for readability, secret shown as literal.
printf '  %s' "${CMD[0]}"
for tok in "${CMD[@]:1}"; do
    case "$tok" in
        --*) printf ' \\\n    %s' "$tok" ;;
        *)   printf ' %q' "$tok" ;;
    esac
done
printf '\n\n'

# ---------------------------------------------------------------------------
# LAUNCH (only with explicit --launch).
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--launch" ]; then
    [ "$TOKEN_PRESENT" = 1 ] || fail "Refusing to launch: HF_TOKEN not exported (results would be lost on the ephemeral container)."
    [ -n "$TRAIN_SCRIPT" ] || fail "Refusing to launch: TRAIN_SCRIPT is unset."

    # H2 (#55 review): verify the token actually has WRITE scope in the output
    # repo's namespace BEFORE spending. A read-only / mis-scoped token passes the
    # length check, trains for ~45 min, then fails at the final push — full spend,
    # zero artifact. Probe by creating + deleting a throwaway private repo in the
    # same namespace (costs $0, takes seconds). Fail loud if it can't.
    echo "[auth] verifying WRITE scope for target repo '${HUB_MODEL_ID}' (idempotent probe)..."
    "${HF_PYTHON:-/opt/homebrew/opt/python@3.11/bin/python3.11}" - "$HUB_MODEL_ID" <<'PYEOF' || fail "Refusing to launch: token lacks WRITE scope for ${HUB_MODEL_ID}. Mint a WRITE-scoped token (WFCA org) and re-export HF_TOKEN."
import sys
from huggingface_hub import HfApi
# Probe the ACTUAL target repo (not a separate probe repo) so a fine-grained
# token scoped to specific repos is tested against the one we truly push to.
# create_repo(exist_ok=True) is idempotent — the repo already exists (created
# with its model card earlier), so this asserts write access without mutating it.
target = sys.argv[1]
a = HfApi()
url = a.create_repo(target, repo_type="model", private=True, exist_ok=True)
print(f"[auth] WRITE scope confirmed for {url} (private, unchanged).")
PYEOF

    echo ">>> --launch passed + write scope confirmed. Submitting to Hugging Face Jobs..."
    exec "${CMD[@]}"
else
    echo "[dry-run] No job submitted. Re-run with --launch to submit (this SPENDS money)."
    echo "[dry-run] Before launching: export HF_TOKEN=\$(hf auth token)."
fi
