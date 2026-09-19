# Runbook — Fine-tune the agent-memory tool-call model on Hugging Face Jobs

**Status:** operator runbook (parametrized). **Last verified:** 2026-07-04.
**Goal:** one finished, self-owned model — end-to-end from a Hub dataset to a
local GGUF loaded in LM Studio that answers a real tool-call.

This runbook is **parametrized above all**: the SAME phases serve a Qwen3-4B
pilot, a Qwen3-8B run, and future iterations. You change *parameters*, not
steps. Two worked invocations (4B pilot and 8B) are at the bottom, driving the
identical phases.

Training now runs on **Hugging Face Jobs** (rented GPU). The Mac
unified-memory swap-thrash failure mode that killed the v5 pilot at step 68 is
**gone** on a real VRAM budget — several Mac-only limits are lifted here and
flagged in the parameter table's "Mac-local vs HF-Jobs" column.

> **Definition-of-Done contract (non-negotiable).** Every GATE below verifies
> the **END STATE with evidence**, not the action. "I ran the command" is not a
> pass. "The job was submitted" is not a pass. A gate passes only when you have
> queried/curled/inspected the real artifact and have the output in hand.

---

## Parameter table (set these; everything downstream reads them)

All parameters are environment variables consumed by
`scripts/fine_tune/run_hf_job.sh` (Phase 2) or passed to the other scripts.
Defaults are the wrapper's defaults.

| Parameter | Default | Meaning | Mac-local vs HF-Jobs |
|---|---|---|---|
| `BASE_MODEL` | `Qwen/Qwen3-4B` | HF repo id of the base model. | **Any size on Jobs.** The former ≤6GB local cap is DELETED (memory `project_hf_jobs_training`). Base choice is capability-driven. |
| `DATASET` | `WFCA/agentmem-toolcalls-v5-pilot` | **Hub** dataset repo id (**PRIVATE**). Jobs run in an isolated container — a local path will NOT work. | Local build stays local; you must PUSH the built dataset to the Hub (Phase 1), and it MUST be private. |
| `DATASET_SPLIT` | `train` | Split to train on. | same |
| `HF_NAMESPACE` | `WFCA` | Hub namespace for outputs (org, paying). `WFCA` (org) or `WFCAMZ` (personal). | Cloud-only concept. |
| `RUN_TAG` | `v5-pilot` | Short tag; becomes part of `HUB_MODEL_ID`. | same |
| `HUB_MODEL_ID` | `WFCA/qwen3-4b-toolcalls-v5-pilot` (pilot) | Output adapter repo id on the Hub (**PRIVATE**). | Cloud-only. |
| `PUSH_PRIVATE` | `1` | `1` = push the adapter PRIVATE (default; data governance). | Cloud-only. |
| `HW_FLAVOR` | `l4x1` | GPU flavor. Verify live: `hf jobs hardware`. | **Cloud-only.** No Mac equivalent. |
| `JOB_TIMEOUT` | `2h` | Hard job timeout. MUST exceed training time — default 30m is too short and loses all progress. | Cloud-only. |
| `MAX_LENGTH` | `4096` | Max tokenized sequence length. | **Mac ceiling of 2048 does NOT apply.** The 2048/GRAD_ACCUM=2 limit in `launch_v5_pilot.sh` existed *only* for Mac swap thrash. On L4/A10G 24GB or A100 80GB, raise it to cover full transcripts. |
| `EPOCHS` | `1.0` | Training epochs. | same |
| `LR` | `2e-4` | Learning rate. | same |
| `LR_SCHEDULER_TYPE` | `cosine` | Scheduler. | same |
| `WARMUP_RATIO` | `0.05` | Warmup fraction. | same |
| `WEIGHT_DECAY` | `0.01` | Weight decay. | same |
| `PER_DEVICE_BATCH` | `2` | Per-GPU micro-batch. | **Raise on Jobs.** On the Mac this was pinned low to avoid thrash; a real GPU can take more. |
| `GRAD_ACCUM` | `4` | Gradient accumulation steps. | On Mac this was inflated (4) to shrink activation memory. On Jobs, lower it and raise `PER_DEVICE_BATCH` for the same effective batch. |
| `EVAL_STEPS` / `SAVE_STEPS` | `50` / `50` | Eval + checkpoint cadence. | same |
| `VALID_SPLIT_PCT` | `0.05` | Held-out eval fraction. | same |
| `LORA_R` / `LORA_ALPHA` / `LORA_DROPOUT` | `16` / `32` / `0.05` | LoRA rank / alpha / dropout. | same (verified locked-in for v5, HANDOFF). |
| `PUSH_MERGED` | `0` | `1` = also push merged (base+LoRA) model, not just the adapter. | Cloud-only. |
| `QUANT` | `Q6_K` | GGUF quantization for the local artifact (Phase 5). | Local step; runs on the Mac after pull-down. |
| `TRAIN_SCRIPT` | `scripts/fine_tune/train_hf_job.py` | Path (or URL) of the TRL/PEFT UV training script. **Local files are uploaded automatically** by `hf jobs uv run` — no pre-publish needed. | Cloud-only. |
| `HF_TOKEN` | *(from `hf auth token`)* | Write token, exported so the job gets it as a secret. Without it the ephemeral container cannot push and the run is lost. | Cloud-only + critical. |

**Hardware flavors + list price (verify live with `hf jobs hardware`):**

| Flavor | GPU | VRAM | $/hr (list) | Good for |
|---|---|---|---|---|
| `l4x1` | 1×L4 | 24 GB | $0.80 | 4B LoRA pilot (cheapest 24GB) |
| `a10g-small` | 1×A10G | 24 GB | $1.00 | 4B/8B LoRA bf16 |
| `a100-large` | 1×A100 | 80 GB | $2.50 | 8B, long context, faster |
| `h200` | 1×H200 | 141 GB | $5.00 | large models / speed |
| `t4-small` | 1×T4 | 16 GB | $0.40 | tiny smoke only |

---

## Phase 0 — Preflight / auth verify

**Inputs:** none.

**Commands:**
```bash
cd /Users/mz/_CODING/agentMemory

# Auth (never echoes the token):
hf auth whoami                       # expect: user=WFCAMZ orgs=WFCA
hf jobs hardware                     # confirm flavor + live pricing

# Export the write token so Phase 2 can pass it to the ephemeral job:
export HF_TOKEN=$(hf auth token)     # value never printed

# Confirm a WRITE token (Jobs + Hub push require a paid plan + write scope).
# Count-only check — do not echo the token:
[ -n "$HF_TOKEN" ] && echo "HF_TOKEN present (${#HF_TOKEN} chars)"
```

**GATE 0 (evidence required):**
- `hf auth whoami` prints `user=WFCAMZ` (paste it).
- `hf jobs hardware` returns a table including your intended `HW_FLAVOR` (paste the row).
- `HF_TOKEN present` prints a non-trivial length. If the token is read-only,
  Phase 6 push will fail — fix now, not later.

**Rollback:** none (read-only). If auth is wrong, `hf auth login` / `hf auth switch`.

---

## Phase 1 — Dataset select / build / push to Hub

The training job reads the dataset **from the Hub**, so a locally-built dataset
must be pushed first. Two sub-paths:

### 1a. Reuse the existing conditioned dataset (fastest)
`datasets/v5_pilot/train.jsonl` already exists (5000 rows, project-conditioned,
redacted, mask-gate-green at 2048 per HANDOFF). For the pilot, reuse it.

### 1b. Rebuild from the DB (if you want fresher / different filtering)
```bash
# Regenerate config interactively (or --defaults), then build:
python3 scripts/fine_tune/v5_pilot_wizard.py --defaults
python3 scripts/fine_tune/build_v5_pilot_dataset.py --config configs/v5_pilot.yaml --dry-run   # inspect funnel
python3 scripts/fine_tune/build_v5_pilot_dataset.py --config configs/v5_pilot.yaml             # write
# Review the audit BEFORE pushing:
cat datasets/v5_pilot/AUDIT.md
```
The builder pulls rows through `scripts/psql_wrapper.sh` (the only sanctioned DB
path), redacts secrets defensively, normalizes paths to `<TRUSTED_ROOT>`, and
writes `train.jsonl` + `AUDIT.md`.

### Push to the Hub
```bash
# Create the dataset repo PRIVATE (idempotent), VERIFY private, THEN upload.
# The data is sensitive/internal — it MUST never be public.
hf repos create "WFCA/agentmem-toolcalls-v5-pilot" --type dataset --private --exist-ok
# Verify private=true BEFORE uploading (do not upload to a public repo):
python3 -c "from huggingface_hub import HfApi; print('private:', HfApi().dataset_info('WFCA/agentmem-toolcalls-v5-pilot').private)"
hf upload "WFCA/agentmem-toolcalls-v5-pilot" datasets/v5_pilot/train.jsonl train.jsonl --type dataset
```

**GATE 1 (evidence required):**
- `wc -l datasets/v5_pilot/train.jsonl` matches the audit's `kept` count.
- `AUDIT.md` funnel shows expected drops and per-project spread (no single project dominating unexpectedly).
- `hf datasets info ${HF_NAMESPACE}/agentmem-v5-pilot` returns the repo with the uploaded file (paste output) — this proves the job will be able to `load_dataset()` it.
- Spot-check redaction: `grep -cE 'hf_[A-Za-z0-9]{20,}|sk-ant-|AKIA[0-9A-Z]{16}' datasets/v5_pilot/train.jsonl` returns **0** (count-only; do not print matches).

**Rollback:** `hf repos delete ${HF_NAMESPACE}/agentmem-v5-pilot --type dataset --yes` (dataset repos are cheap to recreate; nothing else depends on it yet).

---

## Phase 2 — Launch on HF Jobs

**Inputs:** all parameters from the table; `HF_TOKEN` exported; `TRAIN_SCRIPT_URL` set.

> **The training UV script — LOCAL, auto-uploaded.** VERIFIED (2026-07-04):
> `hf jobs uv run SCRIPT` accepts a **local file path** and uploads it to the
> ephemeral container automatically (`hf jobs uv run --help` documents
> "SCRIPT ... local file or URL"). So `scripts/fine_tune/train_hf_job.py` does
> **NOT** need to be pre-published to the Hub — the launcher passes the local
> path and the CLI ships it. (A URL also works if you prefer.) The script
> (`scripts/fine_tune/train_hf_job.py`) is a self-contained PEP723 UV script:
> TRL/PEFT LoRA SFT, chat-template rendering, the **assistant-only span-mask**
> ported from `run_train_lora.py` (deterministic `-100` labels, fail-loud on
> zero-predicted-token rows), and `push_to_hub(private=True)`.

**Dry-run first (default, costs nothing):**
```bash
export HF_TOKEN=$(hf auth token)
BASE_MODEL=Qwen/Qwen3-4B \
DATASET=WFCA/agentmem-toolcalls-v5-pilot \
HUB_MODEL_ID=WFCA/qwen3-4b-toolcalls-v5-pilot \
HW_FLAVOR=l4x1 \
JOB_TIMEOUT=2h \
MAX_LENGTH=4096 \
RUN_TAG=v5-pilot \
  bash scripts/fine_tune/run_hf_job.sh
```
Read the printed PLAN. Confirm base/dataset/flavor/hub_model_id/cost are right.
`TRAIN_SCRIPT` defaults to the local `scripts/fine_tune/train_hf_job.py` (auto-
uploaded). For the 4B pilot, prefer the pre-parameterized
`scripts/fine_tune/launch_pilot_4b.sh` (see `pilot-4b-reboot-checklist.md`).

**Launch (spends money):**
```bash
# same env as above, plus:
  bash scripts/fine_tune/run_hf_job.sh --launch
```
The wrapper refuses to launch if `HF_TOKEN` is unset or `TRAIN_SCRIPT_URL` is
empty (results would be lost on the ephemeral container). It passes the token as
`--secrets "HF_TOKEN=$HF_TOKEN"` so the job can push.

**GATE 2 (evidence required):**
- Dry-run PLAN shows the intended base model, Hub dataset (not a local path), flavor, and a sane cost ceiling.
- After `--launch`: `hf jobs list` shows the new job in `SCHEDULING`/`RUNNING` with the `agentmem-finetune` label (paste the row + job id).

**Rollback:** `hf jobs cancel <JOB_ID>` — stops billing immediately.

---

## Phase 3 — Monitor

**Commands:**
```bash
JOB_ID=<from Phase 2>
hf jobs logs "$JOB_ID" --follow          # stream logs
hf jobs inspect "$JOB_ID"                # status, flavor, timing
# Trackio dashboard URL is printed by the training script at startup.
```
Watch for: loss decreasing, eval running at `EVAL_STEPS`, no OOM/traceback.
Initial logs can lag 30–60s.

**GATE 3 (evidence required):**
- Training loss is trending **down** across the first several evals (paste 2–3 loss lines).
- No `CUDA out of memory` / traceback. If OOM: cancel, lower `MAX_LENGTH` or `PER_DEVICE_BATCH`, or bump `HW_FLAVOR` to more VRAM, re-launch.
- Job reaches `COMPLETED` (`hf jobs inspect` — paste the terminal status). A job killed by timeout is a FAIL: raise `JOB_TIMEOUT` and re-run.

**Rollback:** `hf jobs cancel "$JOB_ID"`.

---

## Phase 4 — Pull adapter + merge

On `COMPLETED`, the training script has pushed the LoRA adapter to
`HUB_MODEL_ID`. Pull it down and merge into the base locally.

**Commands:**
```bash
# Pull the adapter:
hf download "$HUB_MODEL_ID" --local-dir models/lora/_hf_pull/${RUN_TAG}

# Ensure the base is present locally (for the merge):
python3 scripts/fine_tune/download_base.py qwen3-4b        # or qwen3-8b

# Merge adapter -> base and produce the Q6_K GGUF in one chained step:
caffeinate -di .venv-finetune/bin/python scripts/fine_tune/merge_checkpoint.py \
  --checkpoint models/lora/_hf_pull/${RUN_TAG} \
  --base-model models/base/qwen3-4b \
  --out-gguf   models/gguf/agentmem-${RUN_TAG}-q6k.gguf \
  --quant Q6_K
```
`merge_checkpoint.py` chains merge → f16 GGUF → Q6_K quant and cleans the large
intermediates. It is idempotent (skips if the Q6_K already exists).

**GATE 4 (evidence required):**
- `models/lora/_hf_pull/${RUN_TAG}/adapter_model.safetensors` exists (`ls -lh` — paste).
- `merge_checkpoint.py` exits 0 and prints `OK -> ...q6k.gguf (N.NNGB)`.

**Rollback:** delete `models/gguf/agentmem-${RUN_TAG}-q6k.gguf` and the `_merged_*` scratch; re-run. Adapter on the Hub is untouched.

> **Phases 4 and 5 are covered by one script** (`merge_checkpoint.py` does both
> the merge and the GGUF+quant). They are kept as separate gates so each end
> state is verified independently.

---

## Phase 5 — GGUF convert + quant (verify)

The Q6_K GGUF was produced in Phase 4. Verify it as an artifact before trusting it.

**Commands:**
```bash
.venv-finetune/bin/python scripts/fine_tune/verify_gguf.py info  models/gguf/agentmem-${RUN_TAG}-q6k.gguf
.venv-finetune/bin/python scripts/fine_tune/verify_gguf.py smoke models/gguf/agentmem-${RUN_TAG}-q6k.gguf
```
`info` prints arch/params/context/quant and whether the chat template carries
tool-call markers. `smoke` boots `llama-server`, sends 3 prompts, and checks
that ≥2 emit `tool_calls`.

**GATE 5 (evidence required):**
- `verify_gguf.py info` shows the correct architecture, a **present** chat template, and `tool_call markers in template: yes` (paste).
- `verify_gguf.py smoke` reports `>= 2/3 emitted tool_calls` and exits 0 (paste the result line).

**Rollback:** re-quantize (`merge_checkpoint.py --quant Q5_K_M` etc.) or re-merge from a different checkpoint; delete the bad GGUF.

---

## Phase 6 — Push the finished model to the Hub

The **adapter** was already pushed by the training job (Phase 2/4). This phase
optionally publishes the **GGUF** under the configured namespace so it is
downloadable + shareable.

**Commands:**
```bash
hf repos create "${HF_NAMESPACE}/agentmem-${RUN_TAG}-gguf" --type model --private --exist-ok
hf upload "${HF_NAMESPACE}/agentmem-${RUN_TAG}-gguf" \
  models/gguf/agentmem-${RUN_TAG}-q6k.gguf agentmem-${RUN_TAG}-q6k.gguf --type model
```

**GATE 6 (evidence required):**
- `hf models info ${HF_NAMESPACE}/agentmem-${RUN_TAG}-gguf` lists the uploaded GGUF file (paste).
- The size on the Hub matches the local GGUF size (`ls -lh` vs Hub metadata).

**Rollback:** `hf repos delete ${HF_NAMESPACE}/agentmem-${RUN_TAG}-gguf --type model --yes`.

---

## Phase 7 — Pull down + LM Studio smoke test (the finish line)

The Definition of Done: the GGUF loads in LM Studio and answers a **real
tool-call**.

**Commands:**
```bash
# If you pushed to the Hub (Phase 6), pull to a clean location to prove the
# round-trip; otherwise use the local GGUF directly.
hf download "${HF_NAMESPACE}/agentmem-${RUN_TAG}-gguf" \
  --local-dir models/gguf/_hub_pull/${RUN_TAG}

# Drop into LM Studio + run the validator against its OpenAI-compatible server:
bash scripts/fine_tune/lmstudio_smoke.sh \
  models/gguf/_hub_pull/${RUN_TAG}/agentmem-${RUN_TAG}-q6k.gguf 0.5
```
`lmstudio_smoke.sh` copies the GGUF into LM Studio's model dir, waits for you to
load it + start the Local Server, then runs `validate_tool_calls.py` against
`http://localhost:1234/v1`.

**GATE 7 (evidence required — this is the whole point):**
- `curl -s http://localhost:1234/v1/models` lists the loaded model (paste).
- `validate_tool_calls.py` reports a parse-rate at/above the threshold and the model **emits a well-formed tool-call for a real prompt** (paste the passing line).
- Do a manual confirming turn: send one real request in LM Studio (e.g. "list the files in the current directory") and confirm the model returns a `Bash`/`Read`/etc. tool-call, not prose.

**Rollback:** the local test is non-destructive. If it fails, the model is not
done — return to Phase 1 (data) or Phase 2 (hyperparameters) per the failure.
Do NOT declare done on a failed Phase 7.

---

## Worked example A — Qwen3-4B pilot (recommended FIRST run)

```bash
cd /Users/mz/_CODING/agentMemory
export HF_TOKEN=$(hf auth token)

# Phase 1 (reuse existing dataset, push it PRIVATE):
hf repos create WFCA/agentmem-toolcalls-v5-pilot --type dataset --private --exist-ok
hf upload WFCA/agentmem-toolcalls-v5-pilot datasets/v5_pilot/train.jsonl train.jsonl --type dataset

# Phase 2 — SIMPLEST: use the pre-parameterized pilot launcher (dry-run default):
bash scripts/fine_tune/launch_pilot_4b.sh         # inspect PLAN; add --launch to submit

# ...or drive run_hf_job.sh directly:
BASE_MODEL=Qwen/Qwen3-4B \
DATASET=WFCA/agentmem-toolcalls-v5-pilot \
HUB_MODEL_ID=WFCA/qwen3-4b-toolcalls-v5-pilot \
HW_FLAVOR=l4x1 JOB_TIMEOUT=2h \
MAX_LENGTH=4096 EPOCHS=1.0 LR=2e-4 \
RUN_TAG=v5-pilot HF_NAMESPACE=WFCA \
  bash scripts/fine_tune/run_hf_job.sh          # inspect PLAN
# ...then append --launch to submit.
```
**Cost:** l4x1 @ $0.80/hr; a 5000-row 4B LoRA at 1 epoch is well under an hour →
**~$0.50–0.80** typical (2h timeout ceiling = $1.60). This is the cheapest way
to prove the whole pipeline end-to-end.

## Worked example B — Qwen3-8B (same runbook, bigger base)

```bash
# Only the parameters change — identical phases.
BASE_MODEL=Qwen/Qwen3-8B \
DATASET=WFCAMZ/agentmem-v5-pilot \
HW_FLAVOR=a100-large \
JOB_TIMEOUT=3h \
MAX_LENGTH=4096 EPOCHS=1.0 LR=2e-4 \
PER_DEVICE_BATCH=4 GRAD_ACCUM=2 \
RUN_TAG=v6-8b HF_NAMESPACE=WFCAMZ \
TRAIN_SCRIPT_URL=https://huggingface.co/WFCAMZ/agentmem-train-scripts/resolve/main/train_hf_job.py \
  bash scripts/fine_tune/run_hf_job.sh --launch
# Phase 4 merge uses --base-model models/base/qwen3-8b (download_base.py qwen3-8b first).
```
**Cost:** a100-large @ $2.50/hr; 8B LoRA on 5000 rows ≈ 30–60 min → **~$1.25–2.50**
(3h ceiling = $7.50). 8B is a fine pick for its transformer + native tool
template (memory `project_v3_base_model`), no longer gated by any size cap.

**Same runbook, parameters only** — proving parametrization: 4B→8B changed
`BASE_MODEL`, `HW_FLAVOR`, `JOB_TIMEOUT`, batch/accum, `RUN_TAG`, and the Phase-4
base path. Nothing else.

---

## Failure-mode quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| Job dies at timeout | `JOB_TIMEOUT` too short | Raise it; default 30m loses all progress. |
| `CUDA out of memory` | `MAX_LENGTH` / batch too big for VRAM | Lower `MAX_LENGTH` or `PER_DEVICE_BATCH`, or bump `HW_FLAVOR`. |
| Push fails at end of job | `HF_TOKEN` missing/read-only | Export a **write** token before launch; the wrapper warns if unset. |
| `load_dataset` fails in job | dataset not on Hub / wrong split | Phase 1 push; confirm with `hf datasets info`. |
| GGUF smoke emits prose, no tool-calls | chat template / masking issue | Check `verify_gguf.py info` for tool markers; re-check dataset rendering. |
| Model hallucinates cross-project | conditioning regression (the v4 failure) | This is the v5 experiment's whole point — verify `[Project]` block is attended; see methodology-dataset runbook. |
