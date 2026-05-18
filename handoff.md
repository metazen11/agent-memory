# Handoff — agent-memory

## ✅ Current Status (2026-05-18) — V4 SHIPPED

**v4 is the production fine-tuned tool-call model.** Replaces v1 in LM
Studio for daily use. Fixes the v3 tool_response adaptation regression.

| Metric | v1 (was) | v3 (retracted) | **v4 (ship)** |
|---|---:|---:|---:|
| Multi-turn adaptation | 90% | 70% | **100%** |
| Regression rate | 1/10 | 3/10 | **0/10** |
| GGUF Q6_K size | 1.8 GB | 3.1 GB | 3.1 GB |
| Anvil real-run done() | ✓ | partial | ✓ |

See `docs/training_runs/v4-20260518.md` for the full run report.

### v4 artifacts

| Path | Status |
|---|---|
| `models/gguf/qwen3-4b-toolcalls-v4-q6k.gguf` (3.1 GB) | **SHIPPED**, chmod 444 |
| `models/lora/qwen3-4b-toolcalls-lora/runs/20260518T024108Z-v4-full/` (1.2 GB) | adapter kept |
| `~/.lmstudio/models/mz/qwen3-4b-toolcalls-v4/` | loaded, `lms load qwen3-4b-toolcalls-v4` |
| f16 + merged HF intermediates | deleted (regenerable from adapter) |

### What v4 fixes

v3 trained on 4-message rows (`system→user→assistant(tool_call)→tool`).
Model never saw a `tool→assistant` transition, so in real use it re-emitted
the same tool_call after seeing tool_response (loop).

v4 extends each row to 5-message multi-turn when a follow-up assistant
text turn exists in the source `.jsonl`:

```
system → user → assistant(tool_call) → tool(response) → assistant(text)
```

40% of v4's 21k train rows are multi-turn. Trailing text included in
label mask → loss computed on post-tool-response reasoning. Result:
model uses tool_responses to ground text answers (or to choose a
different next tool_call) instead of looping.

---

## 🔄 In progress — v4.5

v4.5 explores both improvements you can push for:

1. **Include `retention_class='live'` rows** (~57k extra) — live
   captured tool_calls from UserPromptSubmit hook, fresher and more
   agentic than backfill-only.

2. **Oversample multi-turn 2.5×** to push multi-turn pct from 40% to
   ≥70%. Keeps single-turn coverage intact.

3. **NEW: error-recovery row tagging.** Find rows where the assistant's
   next tool_call uses a different path/args after a failed previous
   call — directly target the v3 regression pattern. Oversample these 3×.

Goal: prove v4.5 beats v4 on harder real-world tests (longer chains,
MCP tool use, agent-memory queries) or document that v4 is the local
optimum.

Tracked as tasks #22-#25 in `todo.json`.

---

## Branch contract reminder

- Agent work lands on `dev` via reconciler (squash + force-with-lease)
- PRs only on `dev → main` (the human review gate)
- See `~/.claude/agents/reconciler.md`

---

## Reference

- `docs/training_runs/v4-20260518.md` — v4 ship report
- `docs/training_runs/v3-incident-20260515.md` — Dropbox-kill + NaN postmortem
- `docs/fine_tune/V3_PLAN.md` — original v3 spec (now superseded)
- `~/.claude/skills/qwen-finetune/skill.md` — operator runbook
- `scripts/fine_tune/build_v4_dataset.py` — multi-turn dataset builder
- `scripts/fine_tune/ab_multiturn.py` — A/B test harness (llama-server)
