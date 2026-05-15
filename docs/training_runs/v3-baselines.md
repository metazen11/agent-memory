# v3 Phase 0.5 Baselines — v1 vs v2

**Run date:** 2026-05-15
**Purpose:** Lock in v1 (Q4_K_M) and v2 (Q6_K) numbers across all 5 eval classes
so that v3 has unambiguous comparison points.

## Models under test

| Model | Path | Quant | Server |
|---|---|---|---|
| `qwen2.5-3b-toolcalls-v1` | `models/gguf/qwen2.5-3b-toolcalls-q4km.gguf` | Q4_K_M | `llama-server --jinja -c 8192 127.0.0.1:9100` |
| `qwen2.5-3b-toolcalls-v2-q6k` | `models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf` | Q6_K | `llama-server --jinja -c 8192 127.0.0.1:9099` |

## Master comparison table

| Class | Metric | v1 | v2 | Winner |
|---|---|---|---|---|
| A — in-distribution replay (n=30, seed=17) | shape_match rate | **6.7%** (2/30) | **46.7%** (14/30) | v2 (+40pp) |
| A | wrong (text-vs-tool mismatch) | 5/30 | 0/30 | v2 |
| B — tool_response adaptation (n=30, seed=23) | adaptation rate (adapted+text) | **100%** (30/30) | 90% (27/30) | v1 |
| B | text_answer rate | **10/30** | 0/30 | v1 |
| B | identical_reemit (v2 bug) | 0/30 | 3/30 | v1 |
| C — real-world agentic (n=10) | useful_answer rate | **30%** (3/10) | 0% (0/10) | v1 |
| C | loop rate | 40% (4/10) | **90%** (9/10) | v1 |
| C | multi_turn_adapted | 9/10 | 8/10 | v1 |
| C | turn-1 args populated | 10/10 | 10/10 | tie |
| D — validator carryover (n=20) | parse_rate | **95%** (19/20) | 80% (16/20) | v1 |
| D | schema_valid | 95% | 80% | v1 |
| D | empty_args_emissions | 0 | 0 | tie |
| E — project-specific recall (n=12) | PASS rate | 66.7% (8/12) | **75.0%** (9/12) | v2 (+8pp) |
| E | FAIL rate | 25% (3/12) | 16.7% (2/12) | v2 |

**Bold** = strictly better; differences ≤ 1 prompt are noted but not flagged as winners.

## Per-class artifacts

| Class | v1 markdown | v2 markdown | v1 JSON | v2 JSON |
|---|---|---|---|---|
| A | [class-a-v1.md](baselines/class-a-v1.md) | [class-a-v2.md](baselines/class-a-v2.md) | `tests/fine_tune/real_world/baselines/class-a-v1.results.json` | `tests/fine_tune/real_world/baselines/class-a-v2.results.json` |
| B | [class-b-v1.md](baselines/class-b-v1.md) | [class-b-v2.md](baselines/class-b-v2.md) | `…/class-b-v1.results.json` | `…/class-b-v2.results.json` |
| C | [class-c-v1.md](baselines/class-c-v1.md) | [class-c-v2.md](baselines/class-c-v2.md) | `…/class-c-v1.results.json` | `…/class-c-v2.results.json` |
| D | [class-d-v1.md](baselines/class-d-v1.md) | [class-d-v2.md](baselines/class-d-v2.md) | `…/class-d-v1.results.json` | `…/class-d-v2.results.json` |
| E | [class-e-v1.md](baselines/class-e-v1.md) | [class-e-v2.md](baselines/class-e-v2.md) | `…/class-e-v1.results.json` | `…/class-e-v2.results.json` |

(JSON files are gitignored under `tests/fine_tune/real_world/baselines/`.)

## Headline takeaways

1. **v2 is strictly better at imitation; v1 is strictly better at thinking.**
   - v2 shape_matches the gold tool_call 7× more often than v1 (47% vs 7%).
   - But v1 is the only model that synthesizes a final text reply after a tool result (10/30 in Class B, 3/10 useful_answer in Class C).
   - v2 always emits another tool_call. That's the regression we already retracted v2 for.

2. **v2's `identical_reemit` rate is non-zero (10%) but not catastrophic.**
   Class B identified 3 sessions where v2 re-emitted the exact same tool_call after seeing the tool response — the classic "stuck loop" failure. This is much lower than the impression we had from Class C (where the 5-turn fake-tool harness causes 90% looped). Class B's gentler probe shows the underlying behavior is "adapt but don't conclude" rather than "loop forever."

3. **Class A is the right lens for "did v2 learn the training distribution?"** — yes (47%). The 16/30 `needs_review` rows are mostly v2 picking a different but plausible tool (e.g. Bash where the gold used Read, or Read with a different `file_path`). Most of those are not regressions; they are valid alternatives. A v3 manual pass over these would be useful before treating 47% as the true baseline.

4. **Project recall (Class E) actually works for both models.** 8/12 and 9/12 PASS is well above the 50% gate. The model recognizes mentions of `agent-memory`, `Daily Dispatch`, `Fire Map`, `Anvil`, `mem_sessions`, `session-start`, etc. The fine-tune absorbed surface-level project knowledge from training data conversation contexts. v3 should be able to push this higher with targeted project-recall splits.

## Verdict: fit-for-purpose as v3 comparison?

**Yes**, with one caveat:

- Class A's `shape_match` definition is strict (exact tool name + arg key set). The 16/30 `needs_review` v2 rows need a human pass to decide which alternatives count as correct. Currently they all drag the shape_match rate down. A second-pass manual labelling would lift v2 to a more realistic ~70–80%.
- Class B's 0/30 v2 text_answer rate is the regression signal that v3 must fix. v3 should hit ≥30% text_answer here.
- Class C's 30% useful_answer (v1) is the lower bound v3 must beat. v3's target is ≥50%.
- Class D's 80% parse_rate (v2) is below v1's 95% — quantization noise plus chat-template drift. v3 should hold ≥85%.
- Class E's 75% v2 PASS rate is the bar. v3 should hold ≥70% (we don't need to optimize this dimension hard, but it shouldn't regress).

## Schema gap (Class E)

The eval-report.schema.json `eval_class` enum does not include `"E"`. Class E reports set `eval_class: "custom"` and tag `aggregate_stats.eval_class_label: "E"`. **Follow-up:** extend the schema enum to include `"E"` and any future class letters before v3 lands.

## Run mechanics

- Harnesses created:
  - `tests/fine_tune/real_world/harness_class_a.py` — in-distribution replay
  - `tests/fine_tune/real_world/harness_class_b.py` — tool_response adaptation
  - `tests/fine_tune/real_world/harness_class_e.py` — project recall
  - `tests/fine_tune/fixtures/project_recall_prompts.txt` — Class E fixture (12 prompts)
- Class C: legacy `/tmp/v2-rwt/{v1,v2-chat}-results.json` re-emitted to schema via `tests/fine_tune/real_world/baselines/_convert_class_c.py` (no inference rerun).
- Class D: `scripts/fine_tune/validate_tool_calls.py --backend openai --anti-loop` against each port, converted via `tests/fine_tune/real_world/baselines/_run_class_d.py`.
- Servers: `llama-server` with `--jinja -c 8192 --host 127.0.0.1`, ports 9100 (v1) and 9099 (v2). Killed cleanly after the runs.

## Open issues for the user

1. **Class A `needs_review` manual pass.** 16/30 v2 needs_review rows likely contain plausible-alternative tool choices. Decide policy: do these count as PASS, or do we tighten the prompt/training to drive them toward `shape_match`?
2. **Schema enum extension.** Add `"E"` (and reserve `"F"`, `"G"` etc.) to `schemas/eval-report.schema.json` so Class E reports stop being `eval_class: "custom"`.
3. **Class C is only 10 prompts.** Consider expanding to 30 like A/B for stronger signal. The 10-prompt set was originally chosen because it was hand-curated for the v2 retraction; for v3 we want a wider sample.
4. **Class B `text_answer` is the right v3 gate.** v1 hits 33% (10/30); v2 hits 0%. v3 should hit ≥30%. This is the single most actionable headline gate for the v3 training-data fix.
