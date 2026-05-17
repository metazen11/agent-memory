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

# Eval Report: class-a-qwen2.5-3b-toolcalls-v2-q6k-2026-05-15

**Run date:** 2026-05-15
**Eval class:** A

> **Verdict: FAIL [X]** — Class A in-distribution replay: 14/30 shape_match (47%), 0/30 close, 16/30 review, 0/30 wrong.
>
> **Recommendation:** Use this as v3 baseline for in-distribution recall. Inspect `needs_review` rows manually to decide which alternative tool choices are acceptable. Drift versus training data is unexpected and indicates either training/eval template mismatch or undertrained tool selection.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v2-q6k |
| Path | models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf |
| Quant | Q6_K |
| Params | 3B |
| Harness | harness_class_a.py @ phase-0.5-baseline-20260515 |
| Endpoint | /v1/chat/completions |
| Server | llama-server --jinja -c 8192 (http://127.0.0.1:9099) |
| Temperature | 0.0 |
| Max tokens | 512 |
| Max turns | 1 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| shape_match rate | >= 70 | 46.7% | FAIL | 14/30 sessions match tool name + arg keys exactly |
| shape_or_close rate | >= 80 | 46.7% | FAIL | 14/30 shape+close |
| wrong rate | <= 10 | 0.0% | PASS | 0/30 model gave text when training had tool_call (or vice versa) |

## Baseline comparison

_No baselines linked._

## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | wait i still don't understandy why /mz/_coding/anvil doesn't have the... | useful_answer | 1 | — |
| 2 | we didn't get everything to green also... check the health of the geo... | off_topic | 1 | needs_review |
| 3 | we had dailydispatch.local and wfca.local all working with https and... | useful_answer | 1 | — |
| 4 | i just sent a message in telegram and i did not get any response afte... | off_topic | 1 | needs_review |
| 5 | yes - for clusters, but singular fire detections I would assume would... | off_topic | 1 | needs_review |
| 6 | =====================================================================... | off_topic | 1 | needs_review |
| 7 | trusted root needs to be updates to /users/mz/_CODING | useful_answer | 1 | — |
| 8 | ok so materialized views seem to be slowing everything down then? | off_topic | 1 | needs_review |
| 9 | please read the handoff.md and figure out what is next I would like t... | useful_answer | 1 | — |
| 10 | keep going | off_topic | 1 | needs_review |
| 11 | mem flush and github issues to ensure | useful_answer | 1 | — |
| 12 | keep going | useful_answer | 1 | — |
| 13 | also i wanted to revisit the objectives such as -> neris integrations... | off_topic | 1 | needs_review |
| 14 | try again | useful_answer | 1 | — |
| 15 | try again | useful_answer | 1 | — |
| 16 | trusted root needs to be updates to /users/mz/_CODING | off_topic | 1 | needs_review |
| 17 | try again | off_topic | 1 | needs_review |
| 18 | when i filter for SoF in the detections panel, I am not seeing any li... | off_topic | 1 | needs_review |
| 19 | try again | off_topic | 1 | needs_review |
| 20 | mem flush and github issues to ensure | useful_answer | 1 | — |
| 21 | 1. cli_slash.py is 2108 lines — a single function. Untestable, unrevi... | useful_answer | 1 | — |
| 22 | https://wfca.local/fire-map is still not loading - connection is not... | off_topic | 1 | needs_review |
| 23 | try again | useful_answer | 1 | — |
| 24 | ok implement | off_topic | 1 | needs_review |
| 25 | ok go ahead with pr dev to master | useful_answer | 1 | — |
| 26 | ok implement | off_topic | 1 | needs_review |
| 27 | [Image: source: /Users/mz/Dropbox/Screenshots/SCR-20260429-hjya.png] | useful_answer | 1 | — |
| 28 | try again | off_topic | 1 | needs_review |
| 29 | keep going :   Already existed (discovered & closed):   - Loop detect... | off_topic | 1 | needs_review |
| 30 | simple test with local llm? | useful_answer | 1 | — |

## Aggregate stats

| Metric | Value |
|---|---|
| shape_match | 14/30 |
| close_match | 0/30 |
| needs_review | 16/30 |
| wrong | 0/30 |
| error | 0/30 |
| shape_match_rate_pct | 46.7 |
| shape_or_close_rate_pct | 46.7 |
| sample_seed | 17 |

## Notable findings

In-distribution replay: for each session, the model is given the source system+user and the 5 default trained tools, and its turn-1 emission is compared to the gold next-assistant turn from the training data. `shape_match` requires identical tool name + identical arg key set; `close_match` allows partial key overlap with >=50% value match; `needs_review` flags different tool names (could be a plausible alternative); `wrong` flags type mismatch (text vs tool_call).

## Artifacts

- **raw transcripts** — `tests/fine_tune/real_world/baselines/class-a-v2.results.json`
