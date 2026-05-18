# Handoff — agent-memory

## ⚠️ Current Status (2026-05-17) — V3 TRAINED BUT REGRESSED, V4 NEXT

**v3 training completed cleanly** (13h 48m on MPS, exit 0, eval_loss
0.95 → 0.87, no NaN), but smoke testing showed it **does not fix the
tool_response adaptation regression** that retracted v2.

### Root cause of v3 regression (diagnosed 2026-05-17)

**The v3 training dataset is 100% single-turn.** All 22,069 train rows
are 4-message `system → user → assistant(tool_call) → tool(response)`.
Zero rows include the follow-up assistant text turn after the tool
response. The model literally never saw a `tool → assistant` transition
in training — so it cannot have learned to adapt its second tool call
based on what came back from the first.

`build_v3_dataset.py` extracts one row per `mem_tool_calls` entry and
stops at the tool response. The "text-synth oversample" mechanism only
flagged single-turn rows that *could have been* multi-turn — never
actually extended them with the follow-up.

### The deeper data gap

The DB has `mem_tool_calls` (28,599 rows) and `mem_user_prompts` (5,133
rows) but **no table for assistant text responses**. The backfill
pipeline only ingested user prompts and tool calls from `.jsonl`. The
assistant text turns exist in the source `~/.claude/projects/**/*.jsonl`
files but were never imported.

### v3 ship status

- **Not shipping.** GGUF lives at `models/gguf/qwen3-4b-toolcalls-v3-q6k.gguf`
  (3.1 GB, meets ≤6 GB rule), adapter at
  `models/lora/qwen3-4b-toolcalls-lora/runs/20260516T201839Z-v3-full/`.
- v1 remains the production model.

### What v3 DID ship (infrastructure that survives the model change)

This branch is mostly infrastructure that v4 will reuse:

- **Fix #10 zero-label gate**: builder drops rows whose assistant span
  tokenizes to zero non-special tokens under MAX_LENGTH=1024
- **`scripts/fine_tune/preflight.sh`**: dataset/disk/symlink/caffeinate
  checks + zero-label gate verifier (re-runs Fix #10 against on-disk data)
- **`scripts/fine_tune/launch_v3.sh`**: caffeinate wrapper + Dropbox
  symlink refusal + heartbeat + PID file
- **NanGuardAndHeartbeatCallback** in `run_train_lora.py`: fails fast on
  NaN in train/eval loss, writes `heartbeat.txt` every log step
- **`docs/training_runs/v3-incident-20260515.md`**: full postmortem of
  the Dropbox-symlink mid-run kill + NaN-eval false hypothesis
- **Updated `qwen-finetune` skill iron rules**: models/ + .venv-finetune/
  MUST be real local dirs, never Dropbox symlinks; caffeinate -di required
- **GGUF pipeline**: convert + q6k re-quantize works end-to-end on v3

---

## v4 plan — the actual fix

Tracked as tasks #13-#16 in `todo.json`.

### Phase 1 — Backfill assistant text turns (gating dependency)

The DB has no relational record of assistant text responses. Must
re-parse `~/.claude/projects/**/*.jsonl` and extract assistant turns,
especially those following a tool_response. New table:
`mem_assistant_messages(id, session_id, turn_index, content,
has_tool_calls, prev_tool_call_id)`. Est: 2-4h.

### Phase 2 — `build_v4_dataset.py`

Extend the v3 builder to emit 5-message multi-turn rows when a
`tool → assistant(text)` transition exists in the source jsonl:

```
system → user → assistant(tool_call) → tool(response) → assistant(text)
```

Label mask covers the trailing assistant text so loss is computed on
the post-tool-response reasoning. Target distribution:

- ~60% single-turn (current shape, preserves tool-shape learning)
- ~30% two-turn-with-text-response (the new pattern)
- ~10% three-turn (rare adaptation cases)

Preflight audit gate: ≥20% of train rows must have
`messages[-1].role == 'assistant'`. Est: 2h.

### Phase 3 — Retrain

Reuse all v3 infrastructure. ~13h on MPS overnight.

### Phase 4 — Permanent multi-turn A/B harness

Build `tests/fine_tune/multi_turn_ab.py` (10 prompts × up to 5 turns
× 3 models). Score: useful_answer, loop_rate, tool_response_adaptation_rate,
path_correctness. Ship gate: v4 ≥ v1 baseline on all four. Est: 2h
to build, 1h to run.

### Phase 5 — Ship gate

- GGUF Q6_K ≤ 6 GB
- LM Studio integration test
- `chmod 444` shipped GGUF
- Update CHANGELOG, HANDOFF, run report

**Total wall-clock to working v4: ~22 hours (mostly overnight).
Hands-on: ~7-8 hours.**

---

## Branch contract reminder

- Agent work lands on `dev` via reconciler (squash + force-with-lease)
- PRs only on `dev → main` (the human review gate)
- No agent-opened PRs for individual features
- See `~/.claude/agents/reconciler.md` for the full contract

---

## Reference

- `docs/fine_tune/V3_PLAN.md` — the planned-but-only-partially-realized v3 spec
- `docs/training_runs/v3-incident-20260515.md` — Dropbox-kill + NaN postmortem
- `docs/fine_tune/V2_RWT_RETRACTION.md` — the v2 retraction analysis
- `~/.claude/skills/qwen-finetune/skill.md` — operator runbook
