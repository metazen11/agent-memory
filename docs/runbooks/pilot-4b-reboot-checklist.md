# Pilot 4B — "trigger and reboot" checklist

**Goal:** launch the Qwen3-4B v5 pilot LoRA fine-tune on Hugging Face Jobs
(cloud GPU), then **reboot your Mac** and come back to a finished adapter on the
Hub. The job runs on HF infrastructure, fully independent of your Mac.

**Status:** ready for paid launch (awaiting human approval). **Verified:** 2026-07-04.

This is the tight companion to the parametrized runbook
`docs/runbooks/hf-jobs-finetune.md`. That runbook has the full phase gates; this
one is the fast "come back to it" card.

---

## Repos (both PRIVATE — verified `Status: Private`)

| | Repo | Type | Private |
|---|---|---|---|
| Dataset | `WFCA/agentmem-toolcalls-v5-pilot` | dataset | ✅ yes |
| Model (adapter output) | `WFCA/qwen3-4b-toolcalls-v5-pilot` | model | ✅ yes |

The dataset contains sensitive internal content and **must stay private**. Both
repos were created `--private` and verified private before any upload.

---

## 0. One-time before launch

```bash
cd /Users/mz/_CODING/agentMemory
hf auth whoami                 # expect: user=WFCAMZ orgs=WFCA
export HF_TOKEN=$(hf auth token)   # write token; never printed. Needed so the
                                   # ephemeral container can push the adapter.
```

## 1. Dry-run (spends nothing — confirm the plan)

```bash
bash scripts/fine_tune/launch_pilot_4b.sh
```

Read the printed PLAN. Confirm: base=`Qwen/Qwen3-4B`, dataset + output are the
**private WFCA** repos, `push_private: 1`, flavor `l4x1`, timeout `2h`.

## 2. LAUNCH the paid job (human approval required)

```bash
export HF_TOKEN=$(hf auth token)
bash scripts/fine_tune/launch_pilot_4b.sh --launch
```

The launcher refuses to submit unless `HF_TOKEN` is exported. On submit it
prints the job id and the job appears in `hf jobs ps`.

**Cost:** `l4x1` @ ~$0.80/hr. A 5000-row 4B LoRA at 1 epoch runs
**~20-45 min → ~$0.30-0.60**. The `2h` timeout is a safety ceiling only
(worst-case ~$1.60). Real run is well under that.

## 3. REBOOT

Once you have the job id, you can reboot. Training continues on HF's GPU.

---

## 4. Come back — status commands

```bash
hf jobs ps                                  # running jobs
hf jobs list --label agentmem-finetune      # all agentmem jobs + IDs
JOB_ID=<paste>
hf jobs inspect "$JOB_ID"                    # status / flavor / timing
hf jobs logs "$JOB_ID" --follow              # stream logs — loss should trend down
```

**Expected timeline:** ~20-45 min of `RUNNING`, then `COMPLETED`.
A job that hits the 2h timeout is a FAIL (raise `JOB_TIMEOUT` and relaunch).

## 5. How to know it's DONE

The training script pushes the adapter to the private model repo at the end. It
is done when the adapter file exists there:

```bash
hf download WFCA/qwen3-4b-toolcalls-v5-pilot --local-dir models/lora/_hf_pull/v5-pilot
ls -lh models/lora/_hf_pull/v5-pilot/adapter_model.safetensors   # present == done
```

(Also: `hf jobs inspect "$JOB_ID"` shows terminal status `COMPLETED`.)

---

## 6. Next local steps — get the callable local GGUF

Turn the Hub adapter into a GGUF you can load in LM Studio and call:

```bash
# 1. Ensure the base is present locally (for the merge):
python3 scripts/fine_tune/download_base.py qwen3-4b

# 2. Merge adapter -> base and produce a Q6_K GGUF (chained, idempotent):
caffeinate -di .venv-finetune/bin/python scripts/fine_tune/merge_checkpoint.py \
  --checkpoint models/lora/_hf_pull/v5-pilot \
  --base-model models/base/qwen3-4b \
  --out-gguf   models/gguf/agentmem-v5-pilot-q6k.gguf \
  --quant Q6_K

# 3. Verify the GGUF (arch + chat template tool markers, then a 3-prompt smoke):
.venv-finetune/bin/python scripts/fine_tune/verify_gguf.py info  models/gguf/agentmem-v5-pilot-q6k.gguf
.venv-finetune/bin/python scripts/fine_tune/verify_gguf.py smoke models/gguf/agentmem-v5-pilot-q6k.gguf

# 4. Load in LM Studio + run the tool-call validator against its local server:
bash scripts/fine_tune/lmstudio_smoke.sh models/gguf/agentmem-v5-pilot-q6k.gguf 0.5
```

**Done-for-real** = the GGUF loads in LM Studio and emits a well-formed tool
call for a real prompt (see Phase 7 gate in `hf-jobs-finetune.md`).

---

## Rollback / stop the bleed

- Cancel a running job (stops billing immediately): `hf jobs cancel "$JOB_ID"`
- Delete the output model repo: `hf repos delete WFCA/qwen3-4b-toolcalls-v5-pilot --type model --yes`
- The dataset repo is untouched by training; leave it.
