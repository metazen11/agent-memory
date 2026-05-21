# Handoff — agent-memory

> **2026-05-18/19 infra sprint (separate track):** lesson-scope leak fixed,
> session-start preamble shrunk 97%, new `recall()` + `abilities_memory()`
> MCP tools, anvil reached lessons-inject parity with claude, integration
> guide at `docs/INTEGRATION.md`, migration 015 quarantines super-projects
> on fresh DBs, `tool_calls` router mount bug fixed (`/api/tool-calls` was
> 404 since forever), codex per-turn-lessons gap specced at
> `docs/sessions/codex-parity-todo.md`. Full write-up:
> `docs/sessions/2026-05-19-memory-infra.md`. **8 commits on `dev`;
> integration PR #51 (`dev → main`) is open and MERGEABLE
> (fast-forward).** Stream 2 commits in the same PR are the v4/v4.5
> fine-tune sprint — confirm intentional before merging.

## Current Status (2026-05-19 evening) — v5 PILOT KILLED ON MEMORY THRASH, RELAUNCH PENDING APP CLOSURE

**v5 is the data-conditioning experiment.** Real Anvil system prompt as a constant feature + project_name/project_root/subfolder/cwd as per-row features + `<TRUSTED_ROOT>` path normalization. Hypothesis: v4 cross-project hallucination is an attention-conditioning failure, not a capacity/knobs failure. Fixing the data structure should lift cross-project tool selection without changing model size.

v4 remains production. v4.5 was **skipped** — knob-tuning on the same flawed dataset was the wrong cost for the wrong gain.

### What got done today (2026-05-19)

1. ✅ Tightened `content_chars` cap 3500 → **2000** in `scripts/fine_tune/build_v5_pilot_dataset.py:321`.
2. ✅ Rebuilt v5-pilot dataset: 15000 → 5000 clean rows. Funnel: dropped 184 excluded project / 840 continuation / 60 duplicate / 923 cwd outside workspace / 2199 too long.
3. ✅ Per-project distribution: fire-map.wfca.com 1793 / psde_mz_test 1744 / anvil 672 / agentMemory 470 / mz-personal-archived 321.
4. ✅ Split → 4500 train / 500 valid + tiny smoke set.
5. ✅ **Mask gate PASSES at MAX_LENGTH=2048 for both train and valid** (0 bad rows).
6. ✅ Dataset audit confirmed `[Project]` block renders correctly: project_name 100%, project_root 100%, cwd 100%, subfolder 33.6% (92% on fire-map, correct — subfolder only set when cwd is below project_root).
7. ❌ **Launched at MAX_LENGTH=2048 / GRAD_ACCUM=2 → swap-thrashed**. Step time ballooned 10s → 400+s, process state flipped to `UN`, swap hit 48.3 GB of 49 GB, pages_free dropped to 947 (need >100k). Killed at step 68/2250 after ~6h elapsed.
8. ✅ Killed cleanly. After kill: pages_free 5.7M (87 GB freed), swap drained 48 → 31 GB.

### Memory hogs identified — must close BEFORE next launch

| Process | RSS | Notes |
|---|---|---|
| `com.apple.Virtualization.VirtualMachine` (pid 72353) | **6.7 GB** | Apple Virtualization XPC service — Docker Desktop / Container.app / similar. Quit whatever VM is running. |
| `lmstudio/.../llmworker.js` (pid 18474) | **3.3 GB** | LM Studio model worker. Cmd-Q LM Studio. |
| `lmstudio/.../llmworker.js` (pid 31497) | **1.9 GB** | Second LM Studio worker. Cmd-Q LM Studio. |
| LM Studio Helper (Renderer) + main | ~0.5 GB | Closed by Cmd-Q LM Studio. |

**Total recoverable: ~12 GB.** This is the difference between a successful run and another thrash.

### What's verified locked-in (do not change)

- Dataset at `data/processed/qwen3_tools/v5-pilot/{train,valid}.chat.jsonl` (4500/500 rows) is good. Mask gate green at 2048. No need to rebuild.
- v5_schema.py renders project_name/project_root/subfolder/cwd correctly. Other DB fields (`git_branch` 23% coverage, `source_agent` 48%, `source_mode` 61%) are deliberately NOT injected — would muddle the pilot experiment. Defer to v5.1 if pilot lifts.
- Base model: `qwen3-4b`. Trainable params: 33M / 4.05B (0.81%). LoRA r=16 α=32 dropout=0.05.

## NEXT SESSION — RESUME HERE

### Step 0 — Close memory hogs (USER ACTION REQUIRED)

```bash
# Verify what's still hogging:
ps -eo pid,pcpu,pmem,rss,comm | sort -k4 -nr | head -8
pgrep -fl lmstudio
pgrep -fl "Virtualization\|Docker\|Parallels\|qemu"
vm_stat | head -4 ; sysctl vm.swapusage
```

Then:
1. **Quit LM Studio** (Cmd-Q from menu bar). Kills both llmworker.js procs.
2. **Quit Docker Desktop / Container.app / Parallels / whatever VM is running.** The `com.apple.Virtualization.VirtualMachine` proc must be gone.
3. **Wait for swap to drain.** Run `sysctl vm.swapusage` until `used` is < 5 GB or stops dropping.

Gate to proceed: `pages_free > 1,000,000` AND `swap used < 5 GB`.

### Step 1 — Relaunch at safer memory budget

The 2048/grad_accum=2 envelope **failed in practice today** even with the launcher's stated "proven safe" defaults. Drop one notch.

**Recommended config:**

```bash
# In a fresh shell:
cd /Users/mz/_CODING/agentMemory
MAX_LENGTH=1536 GRAD_ACCUM=4 nohup bash scripts/fine_tune/launch_v5_pilot.sh full \
  > /dev/null 2>&1 &
sleep 5
PID=$(cat $(ls -t logs/m-ft-v5-pilot/full-*.pid | head -1))
LOG=$(ls -t logs/m-ft-v5-pilot/full-*.log | head -1)
echo "pid=$PID  log=$LOG"
```

Why 1536 / grad_accum=4:
- Halves the activation memory vs 2048 (sequence length squared in attention).
- grad_accum=4 keeps the same effective batch (2048×2 ≈ 1536×4 ≈ same total tokens/step).
- 1536 tokens still covers ~95% of v5-pilot rows fully (the cap is content_chars=2000 ≈ ~600-800 tokens, plus system prompt overhead).
- Target step time: 8-12s. Target ETA: 5-7h. If step time stays under 15s for the first 50 steps, the run is healthy.

If 1536 still thrashes (it shouldn't, but if):

```bash
MAX_LENGTH=1024 GRAD_ACCUM=4 nohup bash scripts/fine_tune/launch_v5_pilot.sh full \
  > /dev/null 2>&1 &
```

This is the bottom of the safe range. Step time ~5-8s, ETA ~3-5h, some longer rows tail-truncate (acceptable for the pilot).

### Step 2 — Monitor

```bash
# In a tmux pane or this terminal:
tail -F "$LOG" | grep -E "loss|eval|saving|Error|Traceback|killed"

# Memory check every ~10 min in a separate tab:
ps -p $PID -o pid,pcpu,pmem,rss,etime,state
vm_stat | grep "Pages free"
```

Kill criteria (the lessons from today):
- `state=UN` for two consecutive checks → kill, drop MAX_LENGTH.
- `Pages free < 10000` (16k page = 160 MB free) → kill, drop MAX_LENGTH.
- step time > 30s for 5+ consecutive steps → kill, drop MAX_LENGTH.

### Step 3 — Post-train

When training finishes (eval_loss not improving for patience=3 or epoch hits 1.0):

1. Adapter saves to `models/lora/qwen3-4b-toolcalls-lora/runs/<run-tag>/adapter/`.
2. Merge + GGUF: there should be a downstream `merge_and_export.sh` or similar — confirm path in `scripts/fine_tune/` before running.
3. Eval against v4 baseline: `.venv-finetune/bin/python scripts/fine_tune/eval_harder.py --adapter <run-dir>` on all 4 categories.
4. Compare to v4 baseline (in `docs/training_runs/v4-20260518.md`).
5. Decision: if lift on cross-project category → proceed to v5-full on cloud. If no lift → investigate whether `[Project]` block is actually being attended to (probe study).

## What's done in v5 pilot

| File | Purpose |
|---|---|
| `scripts/fine_tune/v5_schema.py` | V5Row dataclass + path normalizer + project block renderer + SYSTEM_PROMPT constant |
| `scripts/fine_tune/v5_pilot_wizard.py` | Interactive config generator → `configs/v5_pilot.yaml` |
| `scripts/fine_tune/build_v5_pilot_dataset.py` | DB query → filter → normalize → render → write jsonl + audit |
| `scripts/fine_tune/v5_split_for_trainer.py` | Split 5k jsonl into trainer-expected `train.chat.jsonl`/`valid.chat.jsonl`/tiny |
| `scripts/fine_tune/launch_v5_pilot.sh` | Wrapper around the qwen2.5-3b trainer with v5/qwen3-4b env vars |
| `configs/v5_pilot.yaml` | Locked-in pilot config (15k window → 5k clean, exclusions, continuation filter, etc) |
| `datasets/v5_pilot/train.jsonl` | 5000 rendered v5 rows |
| `datasets/v5_pilot/AUDIT.md` | Funnel stats, per-project counts, per-subfolder counts, sample rows |
| `data/processed/qwen3_tools/v5-pilot/` | Train/valid + tiny splits the trainer reads |

## Locked-in design decisions (do not relitigate)

1. **Anvil system prompt is a constant feature** (lives in v5_schema.py, not learned). Same idea as why global lessons aren't trained on — runtime-injectable, mutable, redundant in weights.
2. **`<TRUSTED_ROOT>` token + project_name as data** — replaces N per-project special tokens. Path normalizer rewrites `/Users/mz/_CODING/anvil/...` → `<TRUSTED_ROOT>/anvil/...` at row build time. Harness must reverse at inference.
3. **`subfolder` field** in V5Row — first folder under project_root (e.g., `wfca-app` for `<TRUSTED_ROOT>/fire-map.wfca.com/wfca-app/db`). Adds conditioning granularity without exploding tag cardinality.
4. **Conservative project exclusions** (Q2 answer): drop `test`, `my-repo`, `my-project`, `DailyDispatch.local`, `ws_b`, `lib`, single-letter names, `agent-a*`, `ab-anvil-workspace*`. Keep `psde_mz_test` and `mz-personal-archived`.
5. **Continuation-prompt filter** (Q1 answer): drop if `len < 20` AND matches `^(yes|ok|sure|go|yeah|y|n|no|do it|and|also|please|let's|next|continue|more)\b`. Drops standalone "yes" turns that have no context.
6. **Path-sanity gate**: drop rows where cwd is not under `/Users/mz/_CODING` (or other workspace prefix). Catches pytest tmpdir noise.
7. **Strip injected reminder blocks** from captured `prompt_text` — `<agent-memory>`, `<system-reminder>`, hook wrappers. Prevents teaching the model to expect injected text.
8. **Inference-time runtime injection** is the right pattern for volatile state (branch, modified files, open PRs). Tracked as Task #40 for v6. Do NOT add to v5.

## Memory budget — Qwen3-4B on this M-series box

**Revised after 2026-05-19 thrash:** `MAX_LENGTH=2048 / GRAD_ACCUM=2` is NOT safe on its own — it depends on what else is resident.

Today's failed run: MAX_LENGTH=2048, GRAD_ACCUM=2, LoRA r=16/α=32. With LM Studio (~5 GB) + a Virtualization.framework VM (~6.7 GB) eating ~12 GB of unified memory, the run swap-thrashed within 20 steps. Step times went 10s → 50s → 400s+. Process state flipped between `RN` and `UN`. Swap pinned at 48 GB of 49 GB. Killed at step 68/2250.

**The real safe envelope is conditional:**

| If unified memory free at launch | Safe MAX_LENGTH | Safe GRAD_ACCUM |
|---|---|---|
| > 80 GB (LM Studio + VMs closed) | 2048 | 2 |
| 60-80 GB (modest other apps) | 1536 | 4 |
| < 60 GB (lots of other apps) | 1024 | 4 |

Always launch from a clean memory state: pages_free > 1M, swap < 5 GB. If you can't get to that gate, drop MAX_LENGTH.

Kill criteria (live, during training):
- `state=UN` two consecutive checks → kill.
- `pages_free < 10000` → kill.
- step time > 30s for 5+ consecutive steps → kill.

Symptom is NOT thermal — cooling change does not recover the run. The fix is to kill and reduce memory pressure.

If MAX_LENGTH=3072+ is desired later, must also drop GRAD_ACCUM to 1.

Saved as memory: `feedback_v5_pilot_memory_budget.md`.

## After v5 ships

1. **Eval** v5 adapter vs v4 baseline using `scripts/fine_tune/eval_harder.py` on all 4 categories (single-tool, multi-turn adaptation, abstain, cross-project)
2. **If lift**: train v5-full on cloud (RunPod A100 ~$2/hr × 6h = ~$12) with full 90k dataset, then ship as v5
3. **If no lift**: investigate whether the conditioning hypothesis is wrong, or whether the implementation has a bug (e.g., is the [Project] block actually being attended to?)
4. **Then** start Task #40 — v6 runtime-state capture (modified files, open PRs, recent commits)

## v4 production artifacts (unchanged, still authoritative)

| Path | Status |
|---|---|
| `models/lora/qwen3-4b-toolcalls-lora/runs/<v4-run>/merged/qwen3-4b-toolcalls-v4-Q6_K.gguf` | Production GGUF (3.1GB, Q6_K) |
| `models/lora/qwen3-4b-toolcalls-lora/latest` → v4 run dir | Symlink |
| `docs/training_runs/v4-20260518.md` | Full run report |

See `docs/training_runs/` for historical run reports.

## Open tasks

- **#40** [pending] v6 runtime-state capture: schema migration + hook + builder support. Deferred until v5 validates.
