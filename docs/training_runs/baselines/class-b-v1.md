<!--
TEST REPORT TEMPLATE — for scripts/fine_tune/render_test_report.py

Template syntax
---------------
Placeholders use {{double_curly}} syntax. Substitution is performed by
plain string replacement (no Jinja, no loops in-template). Tables and
list-shaped sections are pre-rendered by helper functions in the
generator and substituted as whole blocks.

Required placeholders (always substituted, never empty):
  {{report_id}}              report_id from results.json
  {{run_date}}               run_date (YYYY-MM-DD)
  {{eval_class}}              A | B | C | D | custom
  {{verdict_banner}}          PASS/FAIL banner block (pre-rendered)
  {{model_harness_table}}     compact model+harness table (pre-rendered)
  {{gates_table}}             gates table (pre-rendered)
  {{baseline_section}}        baseline comparison delta section,
                              or "_No baselines linked._" if empty
  {{prompts_table}}           per-prompt outcomes table (pre-rendered)
  {{aggregate_stats_table}}   aggregate stats table (pre-rendered)
  {{notable_findings}}        free-text section; falls back to
                              "_(no findings recorded)_"
  {{artifacts_list}}          bullet list of artifacts; falls back to
                              "_No artifacts recorded._"

Add a new placeholder? Update render_test_report.py's
build_substitutions() in the SAME commit. The script asserts every
{{...}} in this template is substituted before writing the output.
-->

# Eval Report: class-b-qwen2.5-3b-toolcalls-v1-2026-05-15

**Run date:** 2026-05-15
**Eval class:** B

> **Verdict: PASS [OK]** — Class B adaptation: 100% adapt-or-answer, 0% identical re-emit, text_answer 10/30.
>
> **Recommendation:** Compare adaptation_rate and identical_reemit between v1 and v2 to confirm the v2 regression hypothesis (training-data imbalance toward tool_call-terminal turns). v3's training data must include more (user → tc → tool → text-answer) completions, and the identical_reemit gate should drop below 20%.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v1 |
| Path | models/gguf/qwen2.5-3b-toolcalls-q4km.gguf |
| Quant | Q4_K_M |
| Params | 3B |
| Harness | harness_class_b.py @ phase-0.5-baseline-20260515 |
| Endpoint | /v1/chat/completions |
| Server | llama-server --jinja -c 8192 (http://127.0.0.1:9100) |
| Temperature | 0.0 |
| Max tokens | 512 |
| Max turns | 1 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| adaptation rate | >= 70 | 100.0% | PASS | (adapted+text)/total = 20+10/30 |
| identical_reemit rate | <= 20 | 0.0% | PASS | 0/30 sessions re-emitted exactly the prior call (v2 loop bug) |
| text_answer rate | >= 30 | 33.3% | PASS | 10/30 synthesized a final text reply after seeing the tool result |

## Baseline comparison

_No baselines linked._

## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | putoutstanding into github issues if they have none then quality gate... | adapted_tool_call | 4 | — |
| 2 | try again | adapted_tool_call | 4 | — |
| 3 | yes | text_answer | 4 | — |
| 4 | wfca.local keeps reloading in a loop | adapted_tool_call | 4 | — |
| 5 | try again | text_answer | 4 | — |
| 6 | ok docker is up and running | adapted_tool_call | 4 | — |
| 7 | simple test with local llm? | adapted_tool_call | 4 | — |
| 8 | In addition, I would like to see tool use not only in the logs but al... | adapted_tool_call | 4 | — |
| 9 | try again | text_answer | 4 | — |
| 10 | putoutstanding into github issues if they have none then quality gate... | adapted_tool_call | 4 | — |
| 11 | ok so materialized views seem to be slowing everything down then? | text_answer | 4 | — |
| 12 | wait i still don't understandy why /mz/_coding/anvil doesn't have the... | text_answer | 4 | — |
| 13 | so i would like to see the outcomes if there is any data in there ale... | text_answer | 4 | — |
| 14 | ok let's pr to dev and go through ci/cd - | adapted_tool_call | 4 | — |
| 15 | ok the last bit i think would be to surface detections as part of our... | text_answer | 4 | — |
| 16 | please get his working on our current local instance with the current... | adapted_tool_call | 4 | — |
| 17 | ok please create professional tickets for all these issues with prope... | text_answer | 4 | — |
| 18 | putoutstanding into github issues if they have none then quality gate... | adapted_tool_call | 4 | — |
| 19 | keep going :   Already existed (discovered & closed):   - Loop detect... | adapted_tool_call | 4 | — |
| 20 | should we ensure that dropbox is fully merged and working and cleaned... | adapted_tool_call | 4 | — |
| 21 | keep going :   Already existed (discovered & closed):   - Loop detect... | adapted_tool_call | 4 | — |
| 22 | [Image: source: /Users/mz/Dropbox/Screenshots/SCR-20260429-hjya.png] | text_answer | 4 | — |
| 23 | should we be deploying the etl by merging our etl repo changes into d... | text_answer | 4 | — |
| 24 | Base directory for this skill: /Users/mz/.claude/skills/improve  # /i... | adapted_tool_call | 4 | — |
| 25 | ok does our reverse proxy work for dailydispatch.local and fire-map.w... | adapted_tool_call | 4 | — |
| 26 | ok implement | adapted_tool_call | 4 | — |
| 27 | each should have acceptance criteria where the end2end screenshots ma... | adapted_tool_call | 4 | — |
| 28 | keep going | adapted_tool_call | 4 | — |
| 29 | # Simplify: Code Review and Cleanup  Review all changed files for reu... | adapted_tool_call | 4 | — |
| 30 | i just sent a message in telegram and i did not get any response afte... | adapted_tool_call | 4 | — |

## Aggregate stats

| Metric | Value |
|---|---|
| adapted_tool_call | 20/30 |
| text_answer | 10/30 |
| identical_reemit | 0/30 |
| off_topic | 0/30 |
| error | 0/30 |
| adaptation_rate_pct | 100.0 |
| identical_reemit_pct | 0.0 |
| sample_seed | 23 |

## Notable findings

Class B feeds the model the first 3 turns of a real session (user + the gold assistant tool_call + the REAL tool response from training data) and looks at the turn-4 emission. `identical_reemit` is the v2 regression signal: the model re-emits exactly the same tool_call instead of either adapting or synthesizing a text answer. `adapted_tool_call` (different args, or different tool) + `text_answer` both count as healthy adaptation.

## Artifacts

- **raw transcripts** — `tests/fine_tune/real_world/baselines/class-b-v1.results.json`
