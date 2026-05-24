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

# Eval Report: class-d-v2-20260515

**Run date:** 2026-05-15
**Eval class:** D

> **Verdict: PASS [OK]** — Class D validator: parse 80% (16/20), schema_valid 80% (16/20), empty_args_emissions=0.
>
> **Recommendation:** Confirms whether the model can still emit a parseable tool_call at all on a fixed set of canonical prompts. v3 must hold or improve on this baseline; regressions here mean the chat template or tool-call format has drifted.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v2-q6k |
| Path | models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf |
| Quant | Q6_K |
| Params | 3B |
| Harness | scripts/fine_tune/validate_tool_calls.py @ phase-0.5-baseline-20260515 |
| Endpoint | /v1/chat/completions |
| Server | llama-server --jinja -c 8192 (127.0.0.1:9099) |
| Temperature | 0.0 |
| Max tokens | 256 |
| Max turns | 1 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| parse_rate | >= 80 | 80.0% | PASS | 16/20 trials parsed as a valid tool_call |
| schema_valid rate | >= 70 | 80.0% | PASS | 16/20 trials parsed AND validated against the tool's JSON schema |
| native_tool_calls | >= 1 | 16 count | PASS | 16/20 returned via OpenAI tool_calls field (vs <tool_call> in content) |

## Baseline comparison

_No baselines linked._

## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | Show me the contents of /etc/hosts. | useful_answer | 1 | — |
| 2 | Open package.json in the project root. | useful_answer | 1 | — |
| 3 | Print the README.md file. | useful_answer | 1 | — |
| 4 | Create scripts/build.sh and put `echo build` in it. | useful_answer | 1 | — |
| 5 | Save the string 'TODO: ship v2' to a file at /tmp/todo.txt. | useful_answer | 1 | — |
| 6 | Write the line 'hello' to /tmp/notes.md. | useful_answer | 1 | — |
| 7 | Search the repo for the string 'parse_tool_call'. | error | 1 | — |
| 8 | Grep for TODO comments in app/. | useful_answer | 1 | — |
| 9 | Search src/ for the symbol `redact_json`. | error | 1 | — |
| 10 | Change the version string from 1.0.0 to 1.0.1 in package.json. | useful_answer | 1 | — |
| 11 | Replace `localhost` with `127.0.0.1` in config/settings.py. | useful_answer | 1 | — |
| 12 | Rename the function `oldName` to `newName` in src/main.py. | useful_answer | 1 | — |
| 13 | Run `ls -la` in the current directory. | useful_answer | 1 | — |
| 14 | Check git status. | useful_answer | 1 | — |
| 15 | List all running docker containers. | useful_answer | 1 | — |
| 16 | Read the file at /etc/hostname for me. | useful_answer | 1 | — |
| 17 | Run `ls -la /tmp` and tell me what's there. | useful_answer | 1 | — |
| 18 | Search the codebase for any function named `parse_tool_call`. | error | 1 | — |
| 19 | Find all .py files in the src directory. | error | 1 | — |
| 20 | Write a hello world script to /tmp/hi.py. | useful_answer | 1 | — |

## Aggregate stats

| Metric | Value |
|---|---|
| parsed | 16/20 |
| schema_valid | 16/20 |
| native_tool_calls | 16/20 |
| parse_rate_pct | 80.0 |
| valid_rate_pct | 80.0 |
| in_distribution_parsed | 13/15 |
| natural_parsed | 3/5 |
| anti_loop_suppressions | 0 |
| empty_args_emissions_total | 0 |

## Notable findings

Class D is the canonical single-turn validator: each canonical prompt is asked once at T=0.0 and the assistant's first emission is parsed for a tool_call. `parse_rate` is the fraction of prompts that produced any parseable tool_call; `schema_valid` adds the requirement that the call's args validate against the tool's JSON schema. `anti_loop` accounts the empty-args emissions (the v1 bug this fine-tune is meant to fix). Underlying validator: scripts/fine_tune/validate_tool_calls.py.

## Artifacts

- **validator raw report** — `/Users/mz/_CODING/agentMemory/tests/fine_tune/real_world/baselines/_class_d_v2/validate_openai_20260515T155643Z.json`
