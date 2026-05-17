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

# Eval Report: class-c-v1-20260515

**Run date:** 2026-05-15
**Eval class:** C

> **Verdict: FAIL [X]** — Class C real-world agentic: 3/10 useful_answer (30%), 4/10 looped, 3/10 gave_up.
>
> **Recommendation:** Baseline for v3 real-world agentic comparison. v1 establishes the lower-bound behavior we need to at least match; v2's regression here (looped on most prompts) is the headline reason v2 was retracted as a v1 replacement.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v1 |
| Path | models/gguf/qwen2.5-3b-toolcalls-q4km.gguf |
| Quant | Q4_K_M |
| Params | 3B |
| Harness | real_world.harness.py @ phase-0.5-baseline-20260515 (legacy run, schema-only re-emit) |
| Endpoint | /completion |
| Server | llama-server --jinja -c 8192 (127.0.0.1:9100) |
| Temperature | 0.2 |
| Max tokens | 512 |
| Max turns | 5 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| useful_answer rate | >= 50 | 30.0% | FAIL | 3/10 sessions produced a final text reply after at least one tool call |
| loop rate | <= 30 | 40.0% | FAIL | 4/10 sessions detected as looped (3 identical or empty-args calls) |
| multi_turn_adapted | >= 70 | 90.0% | PASS | 9/10 sessions changed tool/args at some turn after turn 1 |
| turn-1 args populated | >= 95 | 100.0% | PASS | 10/10 sessions had non-empty args on turn 1 |

## Baseline comparison

_No baselines linked._

## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | Help me find where the validator system prompt is built. Show me the... | gave_up | 5 | in_args_repetition |
| 2 | What changed in the fine_tune scripts in the last week? | useful_answer | 2 | in_args_repetition |
| 3 | Is there a test that proves the empty-args loop is fixed? Show me. | gave_up | 5 | in_args_repetition |
| 4 | I think there might be a memory leak in the MCP server. Investigate. | looped | 4 | identical_reemit, in_args_repetition |
| 5 | Generate a summary of all GGUF files in this repo and their sizes. | looped | 4 | identical_reemit, in_args_repetition |
| 6 | Find every TODO comment in the Python code and group them by file. | gave_up | 5 | in_args_repetition |
| 7 | What's the difference between v1 and v2 training data? | useful_answer | 3 | in_args_repetition |
| 8 | Walk me through how a training row goes from raw .jsonl to the datase... | looped | 3 | identical_reemit, in_args_repetition |
| 9 | Show me the most recent commit and what it changed. | useful_answer | 2 | in_args_repetition |
| 10 | How do I run the validator? Give me the exact command. | looped | 5 | identical_reemit, in_args_repetition |

## Aggregate stats

| Metric | Value |
|---|---|
| useful_answer | 3/10 |
| looped | 4/10 |
| gave_up | 3/10 |
| text_only_fallback | 0/10 |
| max_turns | 0/10 |
| multi_turn_adapted | 9/10 |
| turn1_args_populated | 10/10 |
| useful_answer_rate_pct | 30.0 |
| loop_rate_pct | 40.0 |
| total_tokens | 17780 |

## Notable findings

Class C reuses the 10-prompt agentic real-world harness (5 turns each, with a harness-faked tool result on each tool_call). `useful_answer` requires the model to emit a final text reply after at least one tool result; `looped` is 3 identical or empty-args calls in a row. Results re-emitted into schema form; no inference rerun.

## Artifacts

- **raw transcripts (legacy)** — `/tmp/v2-rwt/...`
