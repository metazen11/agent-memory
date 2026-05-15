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

# Eval Report: class-e-qwen2.5-3b-toolcalls-v2-q6k-2026-05-15

**Run date:** 2026-05-15
**Eval class:** custom

> **Verdict: PASS [OK]** — Class E recall: 9/12 PASS, 1/12 PARTIAL, 2/12 FAIL.
>
> **Recommendation:** Use these baselines to measure v3's project knowledge absorption. If v1/v2 are both mostly FAIL, project-specific recall was never a training signal — that's fine; for v3, add a dedicated project-recall split to the dataset (mem_observations + filename-bearing prompts) and re-measure.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v2-q6k |
| Path | models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf |
| Quant | Q6_K |
| Params | 3B |
| Harness | harness_class_e.py @ phase-0.5-baseline-20260515 |
| Endpoint | /v1/chat/completions |
| Server | llama-server --jinja -c 8192 (http://127.0.0.1:9099) |
| Temperature | 0.0 |
| Max tokens | 512 |
| Max turns | 1 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| PASS rate | >= 50 | 75.0% | PASS | 9/12 responses mention concrete project files/concepts |
| FAIL rate | <= 30 | 16.7% | PASS | 2/12 responses are generic with no project-specific signal |

## Baseline comparison

_No baselines linked._

## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | how do we run QA in agent-memory? | useful_answer | 1 | — |
| 2 | what's the dispatch in Daily Dispatch? | useful_answer | 1 | — |
| 3 | where does the fire map data come from? | text_answer | 1 | — |
| 4 | show me a TDD-style test for a memory observation | text_only_fallback | 1 | — |
| 5 | what tool would you use to check Anvil's TUI bindings? | useful_answer | 1 | — |
| 6 | describe the v2 retrain pipeline | text_only_fallback | 1 | — |
| 7 | what's the empty-args loop bug? | useful_answer | 1 | — |
| 8 | show me how Fire Map's layer panel resolves user permissions | useful_answer | 1 | — |
| 9 | what does Daily Dispatch store in mem_sessions? | useful_answer | 1 | — |
| 10 | how does Anvil's MCP differ from agent-memory's MCP? | useful_answer | 1 | — |
| 11 | what files run on session-start for the agent-memory hooks? | useful_answer | 1 | — |
| 12 | how do you build the v2 training dataset? | useful_answer | 1 | — |

## Aggregate stats

| Metric | Value |
|---|---|
| PASS | 9/12 |
| PARTIAL | 1/12 |
| FAIL | 2/12 |
| ERROR | 0/12 |
| pass_rate_pct | 75.0 |
| eval_class_label | E |

## Notable findings

Class E probes whether the fine-tune absorbed project-specific recall (file names, module names, concept names from agentMemory / Daily Dispatch / Fire Map / Anvil). PASS requires at least one concrete-token hit from the SPECIFIC_TOKENS allow-list; PARTIAL requires only a project-name hit; FAIL means generic text with no project signal at all. The schema enum does not include 'E', so eval_class is set to 'custom' (aggregate_stats.eval_class_label='E'); extending the schema enum is tracked as an open follow-up.

## Artifacts

- **raw transcripts** — `tests/fine_tune/real_world/baselines/class-e-v2.results.json`
