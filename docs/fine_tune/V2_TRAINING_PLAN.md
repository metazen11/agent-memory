# V2 Training Execution Plan (Issue #33)

**Branch:** `feat/v2-finetune-data-pipeline`
**Repo:** `/Users/mz/_CODING/agentMemory`
**Created:** 2026-05-13
**Status:** READY TO EXECUTE pending 3 pre-flight fixes (B1–B3)

This is the actionable plan for #33 — the only open sub-issue of v2 parent #25.
Produced by the Plan agent + quality-gate review on 2026-05-13. Supersedes
HANDOFF.md's command examples (which had argparse-style flags that do not
exist in the training script).

---

## Critical corrections vs. HANDOFF.md

| HANDOFF said | Reality |
|---|---|
| `run_train_lora.py --dataset-dir ... --epochs 1 --run-tag v2` | Script is **env-var driven, no argparse**. Use `DATASET_VERSION=v2 DATASET_TIER=full RUN_TAG=v2-full`. |
| `models/lora/qwen2.5-3b-toolcalls-lora/` (output) | Training **script** lives there. **Output** goes to `models/lora/qwen2.5-3b-instruct-toolcalls-lora/` (with `-instruct-`). |
| "64 fine_tune tests" | Actual collection: **34 tests**. Investigate HANDOFF claim before declaring done. |

---

## Pre-flight blockers (must fix before Phase 1)

- **B1 — `tests/fine_tune/fixtures/vague_prompts.txt` does not exist.** Required for the 0-loop acceptance criterion. Plan agent drafted 50 prompts (Phase 2).
- **B2 — `empty_args_emissions_total` counter is NOT wired into `/api/stats`.** Currently in-process inside the validator only. Either wire it before training or downgrade the acceptance criterion to "validator JSON reports 0 emissions" and file a follow-up.
- **B3 — `MANIFEST.json` lacks `pii_substitutions` field.** 23,983 real user prompts ingested without recorded scrub evidence. Verify scrubbing happened during backfill (#28); if so, document; if not, scrub before training.

---

## Phase 0 — Pre-flight (Dropbox still running)

```bash
cd /Users/mz/_CODING/agentMemory

# Repo state
git status                                       # clean, on feat/v2-finetune-data-pipeline
git log --oneline -1                             # 8621fdb (latest merge)

# Dataset presence
wc -l data/processed/qwen25_tools/v2/*.jsonl     # train=23983, valid=1588, tiny 200/30
jq '.row_counts'  data/processed/qwen25_tools/v2/MANIFEST.json
jq 'keys | length' data/processed/qwen25_tools/v2/tool_schemas.json  # 35

# Base model + binaries
ls models/base/qwen2.5-3b-instruct/REVISION.txt
ls models/llama.cpp/build/bin/{llama-cli,llama-quantize,llama-server}

# Disk
df -Pg . | awk 'NR==2 {print $4 " GB free"}'    # need ≥60 GB

# Python + MPS
.venv-finetune/bin/python -c "import torch; print('MPS:', torch.backends.mps.is_available())"

# Existing tests
.venv-finetune/bin/python -m pytest tests/fine_tune/ -q

# GitHub auth
gh auth status                                   # metazen11 active (for push)

# v2 GGUF must NOT exist yet
test ! -f models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf
```

**Gate:** all green; v2 GGUF target does not exist; ≥60 GB free.
**Rollback:** none — read-only.
**Time:** 10 min.

---

## Phase 1 — Quit Dropbox

`models/` and `.venv-finetune/` are symlinks into Dropbox cold storage.
Sync during training can corrupt checkpoints.

```bash
osascript -e 'tell application "Dropbox" to quit'
sleep 5
pgrep -f "Dropbox.app/Contents/MacOS/Dropbox$" && echo FAIL || echo OK

# Verify symlinks still resolve (Dropbox folder stays mounted; only the sync daemon stops)
ls -L models/base/qwen2.5-3b-instruct/REVISION.txt
ls -L .venv-finetune/bin/python
```

**Gate:** no main Dropbox process; symlinks resolve.
**Rollback:** `open -a Dropbox`.
**Time:** 2 min.

---

## Phase 2 — Create `tests/fine_tune/fixtures/vague_prompts.txt`

50 prompts modeled on Failure Mode #11 (the v1 empty-args loop bug).
Mix of 5 categories × 10 prompts each:

- **Codebase exploration:** "find the fire-map codebase", "show me where auth is wired up", "locate the migrations directory", "what tests cover the backfill", "open the runbook", "where is the LoRA training script", "find the hook that captures user prompts", "show me the schema for mem_tool_calls", "where does the validator live", "find the GGUF conversion command".
- **Build/test debugging:** "why is pytest failing", "run the fine_tune tests", "check that migration 012 applied", "see why the hook errored", "debug the backfill crash", "check linkage_ratio_24h", "tail the training log", "what's in logs/m-ft-2", "verify llama-cli works", "smoke test the base model".
- **File finding:** "open the v2 manifest", "show me FAILURE_MODES", "find every README in the repo", "list the migrations", "open hooks/pre-tool-use.js", "find the anti-loop test", "show me handoff.md", "where's the tool_schemas json", "find dropbox-path leakage", "list everything under docs/fine_tune".
- **Vague refactor/edit:** "clean up the backfill script", "make the validator faster", "add a docstring to render_with_assistant_mask", "split the long Bash module", "rename the v1 GGUF to keep it", "add a comment explaining the assistant mask", "tighten the redact_json patterns", "factor out the manifest writer", "drop the unused fallback schemas", "add type hints to lib.py".
- **Tool-pick edge cases (some should NOT trigger a tool):** "what time is it", "summarize this conversation", "explain LoRA in one line", "is Dropbox running", "git status real quick", "how many rows in v2", "show the latest commit", "did the migration finish", "any failed tests", "what's the v1 GGUF path".

**Gate:** file exists with 50 non-empty lines; loaded successfully by Phase 9 client.
**Rollback:** `rm tests/fine_tune/fixtures/vague_prompts.txt`.
**Time:** 15 min.

---

## Phase 3 — Tiny training (integration gate)

```bash
mkdir -p logs/m-ft-2

DATASET_TIER=tiny DATASET_VERSION=v2 RUN_TAG=v2-tiny-smoke \
  .venv-finetune/bin/python -u models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py \
  2>&1 | tee logs/m-ft-2/train-v2-tiny.log

# Merge tiny adapter
.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model models/base/qwen2.5-3b-instruct \
  --lora-adapter models/lora/qwen2.5-3b-instruct-toolcalls-lora/latest \
  --output-dir models/merged/qwen2.5-3b-toolcalls-v2-tiny-merged

# Tiny GGUF (v2-tagged path — never overwrite v1)
.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen2.5-3b-toolcalls-v2-tiny-merged \
  --out-f16  models/gguf/qwen2.5-3b-toolcalls-v2-tiny-f16.gguf \
  --out-quant models/gguf/qwen2.5-3b-toolcalls-v2-tiny-q4km.gguf \
  --quant Q4_K_M --run
```

**Gate:** training exits 0; `latest` symlink advanced; tiny GGUF > 1 GB.
**Rollback:** delete the new run dir + tiny merged + tiny GGUFs; reset `latest` symlink to prior run (`runs/20260513T070938Z-full-v1`).
**Time:** 25-40 min.

---

## Phase 4 — Tiny validator (≥3% parse rate)

```bash
.venv-finetune/bin/python -u scripts/fine_tune/validate_tool_calls.py \
  --backend llama-cli \
  --gguf models/gguf/qwen2.5-3b-toolcalls-v2-tiny-q4km.gguf \
  --min-parse-rate 0.03 \
  --report-dir logs/m-ft-2 \
  2>&1 | tee logs/m-ft-2/validate-v2-tiny.log
```

**Gate:** validator prints `PASS`; ≥3% parse rate; JSON report saved.
**Rollback:** rollback Phase 3; do NOT proceed to Phase 5.
**Time:** 10 min.

---

## Phase 5 — Full training (~3-4 hours)

```bash
DATASET_TIER=full DATASET_VERSION=v2 RUN_TAG=v2-full \
  .venv-finetune/bin/python -u models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py \
  2>&1 | tee logs/m-ft-2/train-v2-full.log
```

Default `EPOCHS=1.0`, `MAX_LENGTH=1024`, `EVAL_STEPS=500`, `SAVE_STEPS=250`
(pinned per FAILURE_MODES.md #8). `nan` eval_loss is cosmetic (FAILURE_MODES #9).

**Gate:** training exits 0; `latest` repointed; checkpoints in `runs/<UTC>-v2-full/`.
**Rollback:** training auto-resumes from last checkpoint on transient fail.
On hard fail: revert `latest` symlink, archive failed run, do NOT convert to GGUF.
**Time:** 3-4 h wall clock.

---

## Phase 6 — Full validator (≥85% gate)

Validate the merged HF model BEFORE GGUF conversion. Catches training vs.
conversion regressions separately.

```bash
.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model models/base/qwen2.5-3b-instruct \
  --lora-adapter models/lora/qwen2.5-3b-instruct-toolcalls-lora/latest \
  --output-dir models/merged/qwen2.5-3b-toolcalls-v2-merged

.venv-finetune/bin/python -u scripts/fine_tune/validate_tool_calls.py \
  --backend hf \
  --hf-model-dir models/merged/qwen2.5-3b-toolcalls-v2-merged \
  --min-parse-rate 0.85 \
  --report-dir logs/m-ft-2 \
  2>&1 | tee logs/m-ft-2/validate-v2-full-hf.log
```

**Gate:** validator `PASS` at ≥85%.
**Rollback:** if <85%, archive run under `runs/<UTC>-v2-full-rejected/`, revert `latest`. Do not convert to GGUF.
**Time:** 30-45 min.

---

## Phase 7 — GGUF conversion + Q4_K_M (v2 path — never overwrite v1)

```bash
# Hard guard: v2 target must not yet exist
test ! -f models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf || { echo "v2 GGUF exists — abort"; exit 1; }

# Optional: write-protect v1 to enforce non-overwrite
chmod 444 models/gguf/qwen2.5-3b-toolcalls-q4km.gguf
chmod 444 models/gguf/qwen2.5-3b-toolcalls-q4km.gguf.sha256 2>/dev/null || true

.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen2.5-3b-toolcalls-v2-merged \
  --out-f16  models/gguf/qwen2.5-3b-toolcalls-v2-f16.gguf \
  --out-quant models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf \
  --quant Q4_K_M --run

shasum -a 256 models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf \
  | tee models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf.sha256

# Re-validate against the actual ship artifact
.venv-finetune/bin/python -u scripts/fine_tune/validate_tool_calls.py \
  --backend llama-cli \
  --gguf models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf \
  --min-parse-rate 0.85 \
  --report-dir logs/m-ft-2 \
  2>&1 | tee logs/m-ft-2/validate-v2-full-gguf.log
```

**Gate:** v1 GGUF byte-identical to pre-run SHA; v2 GGUF + sha256 sidecar present; llama-cli validator PASS ≥85%.
**Rollback:** `rm models/gguf/qwen2.5-3b-toolcalls-v2*` (v1 intact). If quant degraded parse rate, try Q5_K_M.
**Time:** 15 min.

---

## Phase 8 — LM Studio smoke

```bash
mkdir -p ~/.lmstudio/models/mz/qwen2.5-3b-toolcalls-v2
cp models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf ~/.lmstudio/models/mz/qwen2.5-3b-toolcalls-v2/

# Manual: open LM Studio, load qwen2.5-3b-toolcalls-v2, start OpenAI server on :1234
# Then:
.venv-finetune/bin/python -u scripts/fine_tune/validate_tool_calls.py \
  --backend openai --base-url http://localhost:1234/v1 \
  --model qwen2.5-3b-toolcalls-v2 \
  --min-parse-rate 0.85 \
  --anti-loop --model-version v2 \
  --report-dir logs/m-ft-2 \
  2>&1 | tee logs/m-ft-2/validate-v2-lmstudio.log
```

**Gate:** openai-backend PASS ≥85%; bonus `native_tool_calls > 0`.
**Rollback:** `rm -rf ~/.lmstudio/models/mz/qwen2.5-3b-toolcalls-v2/`. v1 LM Studio entry stays loadable.
**Time:** 15 min.

---

## Phase 9 — Chat-loop verification via llama-server (NEW)

User-requested: confirm v2 in a real interactive loop on the actual GGUF
before restarting Dropbox. **llama-server** chosen because it consumes the
same GGUF that LM Studio loads — same inference path, no MLX re-conversion.

```bash
# Serve v2 GGUF
models/llama.cpp/build/bin/llama-server \
  -m models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf \
  -c 4096 --jinja --port 8088 \
  > logs/m-ft-2/llama-server-v2.log 2>&1 &
SERVER_PID=$!
sleep 8
curl -s http://localhost:8088/v1/models | jq .

# Chat-loop client (5 turns max; fails on 3 consecutive identical calls or empty args)
.venv-finetune/bin/python - <<'PY'
import json, requests
URL = "http://localhost:8088/v1/chat/completions"
schemas = json.load(open("data/processed/qwen25_tools/v2/tool_schemas.json"))
tools = [{"type":"function","function":{"name":n,"description":f"Tool {n}",
        "parameters":{"type":"object","properties":s.get("properties",{}),
                      "required":s.get("required",[])}}}
        for n,s in list(schemas.items())[:8]]
prompts = [p.strip() for p in open("tests/fine_tune/fixtures/vague_prompts.txt")
           if p.strip() and not p.startswith("#")]
loops = empty = 0
for p in prompts:
    msgs = [{"role":"user","content":p}]
    seen = []
    for _ in range(5):
        r = requests.post(URL, json={"model":"v2","messages":msgs,"tools":tools,
                                     "temperature":0.2,"max_tokens":256},
                          timeout=60).json()
        m = r["choices"][0]["message"]
        msgs.append(m)
        tcs = m.get("tool_calls") or []
        if not tcs: break
        sig = tuple((tc["function"]["name"], tc["function"]["arguments"]) for tc in tcs)
        if all(tc["function"]["arguments"] in ("", "{}") for tc in tcs):
            empty += 1
        seen.append(sig)
        if len(seen) >= 3 and seen[-1] == seen[-2] == seen[-3]:
            loops += 1
            break
        msgs += [{"role":"tool","tool_call_id":tc.get("id",""),
                  "name":tc["function"]["name"],
                  "content":"(stub result)"} for tc in tcs]
print(f"prompts={len(prompts)} empty_args_emissions={empty} loops_detected={loops}")
assert loops == 0, "empty-args infinite loop detected"
PY

kill $SERVER_PID
```

**Gate:** `loops_detected == 0` across all 50 prompts; `empty_args_emissions ≤ 1`.
**Rollback:** kill server; treat as Phase 5 regression; revert `latest`; do NOT restart Dropbox.
**Time:** 20-30 min.

---

## Phase 10 — Restart Dropbox

Only after Phases 8 AND 9 both pass.

```bash
open -a Dropbox
sleep 30
pgrep -f "Dropbox.app/Contents/MacOS/Dropbox$" && echo OK

# Restore write perms on v1 GGUF (Phase 7 chmod 444)
chmod 644 models/gguf/qwen2.5-3b-toolcalls-q4km.gguf
```

**Gate:** Dropbox running; sync resumes; v1 GGUF writable again.
**Time:** 5 min.

---

## Phase 11 — Run report + PR

```bash
# Run report at docs/training_runs/v2-<UTC>.md
# Include: final loss, eval loss curve, validator pass rate per backend,
# v2 GGUF SHA, wall clock, 0-loop transcript count.

# Update HANDOFF.md: replace argparse-style commands with env-var form
# Update docs/fine_tune/V2_DATA_PIPELINE_PLAN.md: tick Step 7 checkboxes

gh auth switch --user metazen11
git add docs/training_runs/v2-*.md \
        tests/fine_tune/fixtures/vague_prompts.txt \
        docs/fine_tune/V2_TRAINING_PLAN.md \
        logs/m-ft-2/ \
        HANDOFF.md docs/fine_tune/V2_DATA_PIPELINE_PLAN.md
git commit -m "feat(fine-tune): retrain v2 — empty-args loop fix (#33)"
git push -u origin feat/v2-finetune-data-pipeline
gh pr view 2>&1 || gh pr create --base main --title "feat(fine-tune): v2 retrain (#33)" \
                                --body "Closes #33 and v2 parent #25."
```

**Gate:** PR has run report, validator JSON refs, v2 GGUF SHA, 0-loop evidence.
**Time:** 30 min.

---

## Total time budget

Pre-flight (10m) + Dropbox quit (2m) + fixture (15m) + tiny (25-40m + 10m) +
**full (3-4h + 30-45m)** + GGUF (15m) + LM Studio (15m) + chat-loop (20-30m) +
restart (5m) + docs/PR (30m).

**~5-6 hours wall clock**, with the 3-4h full-training run being the dominant
block. Most of that is unattended.

---

## Known gaps (file as follow-ups, do not block #33)

- HANDOFF claim of 64 tests vs. actual 34. Investigate after training.
- 6 files in `scripts/fine_tune/` still use `.resolve()` (FAILURE_MODES #1 violation).
- `empty_args_emissions_total` counter not wired into `/api/stats` (validator-only).
- v1 f16 GGUF (6.2 GB orphan) in `models/gguf/` — clean up after Phase 7.
- PIPELINE_RUNBOOK.md `convert_to_gguf.py` example would overwrite v1 — update with v2 names.
