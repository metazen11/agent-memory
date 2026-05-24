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

# Eval Report: v2-chat-template-retest-20260515

**Run date:** 2026-05-15
**Eval class:** C

> **Verdict: FAIL [X]** — v2 is genuinely worse than v1 at the agentic task; the harness was not the problem.
>
> **Recommendation:** Retraction stands — do not ship v2 as a replacement for v1. v3 must fix two regressions: (a) never-stops-tool-calling after tool_response (likely training-data imbalance toward tool_call-terminal turns), and (b) within-argument generative loops (likely a long-shell-script data leak). Both are tracked in V3_PLAN.md §6 as Class B / Class C / in-args repetition gates.

## Model & harness

| Field | Value |
|---|---|
| Model ID | qwen2.5-3b-toolcalls-v2-q6k |
| Path | models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf |
| Quant | Q6_K |
| Params | 3B |
| Harness | harness_chat.py @ tmp-2026-05-15 |
| Endpoint | /v1/chat/completions |
| Server | llama-server --jinja -c 8192 (127.0.0.1:9099) |
| Temperature | 0.0 |
| Max tokens | 512 |
| Max turns | 5 |

## Gates

| Gate | Threshold | Actual | Result | Notes |
|---|---|---|---|---|
| useful_answer rate | >= 50 | 0% | FAIL | 0/10 — model never synthesises a final text reply after tool results |
| loop rate | <= 30 | 90% | FAIL | 9/10 — identical tool_call re-emission or in-args generative loop |
| multi_turn_adapted | >= 80 | 80% | PASS | 8/10 — OpenAI tool role does coax varied calls, but adaptation does not lead to text answer |
| turn-1 args populated | >= 95 | 100% | PASS | 10/10 — v2 fine-tune fixed the empty-args bug from v1; this part works |
| in-args repetition violations | 0 violations | 5 violations | FAIL | 5/10 — arguments string hit max_tokens with hundreds of repeated lines (new failure mode vs v1) |
| post-tool_call scaffolding | 0 violations | 0 violations | PASS | Model did not emit fake user/tool_response/assistant scaffolding under --jinja |

## Baseline comparison

| Metric | This run | v1 ChatML (qwen2.5-3b-toolcalls-v1) | Δ vs v1 ChatML (qwen2.5-3b-toolcalls-v1) | v2 ChatML (hand-rolled harness) | Δ vs v2 ChatML (hand-rolled harness) |
|---|---|---|---|---|---|
| useful_answer | 0/10 | 3/10 | -30.0pp | 0/10 | +0.0pp |
| looped | 9/10 | 4/10 | +50.0pp | 9/10 | +0.0pp |
| gave_up | 1/10 | 3/10 | -20.0pp | 0/10 | +10.0pp |
| text_only_fallback | 0/10 | 0/10 | +0.0pp | 1/10 | -10.0pp |
| multi_turn_adapted | 8/10 | 9/10 | -10.0pp | 3/10 | +50.0pp |
| turn1_args_populated | 10/10 | 10/10 | +0.0pp | 9/10 | +10.0pp |
| in_args_repetition_violations | 5/10 | _(n/a)_ | — | _(n/a)_ | — |

- `v1 ChatML (qwen2.5-3b-toolcalls-v1)` results: /tmp/v2-rwt/v1-results.json
- `v2 ChatML (hand-rolled harness)` results: /tmp/v2-rwt/v2-results.json


## Per-prompt results

| # | Prompt (truncated) | Outcome | Turns | Regressions |
|---|---|---|---|---|
| 1 | Help me find where the validator system prompt is built. Show me the... | looped | 5 | in_args_repetition |
| 2 | What changed in the fine_tune scripts in the last week? | looped | 3 | identical_reemit |
| 3 | Is there a test that proves the empty-args loop is fixed? Show me the... | looped | 4 | — |
| 4 | I think there might be a memory leak in the MCP server. Investigate. | looped | 5 | in_args_repetition |
| 5 | Generate a summary of all GGUF files in this repo and their quants. | looped | 4 | — |
| 6 | Find every TODO comment in the Python code and group them by file. | max_turns | 5 | in_args_repetition |
| 7 | What's the difference between v1 and v2 training data? | looped | 3 | identical_reemit |
| 8 | Walk me through how a training row goes from raw .jsonl to the final... | looped | 5 | in_args_repetition |
| 9 | Show me the most recent commit and what it changed. | looped | 4 | identical_reemit |
| 10 | How do I run the validator? Give me the exact command. | looped | 4 | in_args_repetition |

## Aggregate stats

| Metric | Value |
|---|---|
| useful_answer | 0/10 |
| looped | 9/10 |
| gave_up | 1/10 |
| text_only_fallback | 0/10 |
| multi_turn_adapted | 8/10 |
| turn1_args_populated | 10/10 |
| in_args_repetition_violations | 5/10 |

## Notable findings

### Failure pattern (representative)

Prompt 9 ("Show me the most recent commit and what it changed."):

```
turn 1: Bash(command="git log --oneline -3")        -> fake stdout returned
turn 2: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  -> fake content
turn 3: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  [same args]
turn 4: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  [same args] -> looped
```

The model had everything it needed at turn 1's tool result to answer in plain text. Instead it asked for a second file, then re-asked for the same file twice. This is a synthesis failure, not a format issue.

### In-args generative loop (new vs v1)

On 5/10 prompts the model produced a `tool_call` whose JSON arguments string was so long it hit `max_tokens=512` mid-string, with hundreds of repeated lines (e.g. dozens of duplicated `print(...)` statements in a single `python3.11 -c "..."` one-liner). v1 never produced this; v2 produces it on 50% of prompts. This drives the new in-args repetition gate added to V3_PLAN.md §6.

### What did NOT regress

- `--jinja` accepted the GGUF's embedded chat_template cleanly; no silent fallback
- `finish_reason="tool_calls"` returned correctly on tool-emitting turns
- OpenAI `tool` role messages accepted without complaint
- turn-1 args populated 10/10 (the v2 win — empty-args bug is fixed)
- Multi-turn adaptation rose 3/10 → 8/10 vs the ChatML run; adaptation does not translate to useful answers, but the prompt rendering itself is not the bug

## Artifacts

- **chat-completions harness** — `/tmp/v2-rwt/harness_chat.py` (7,191 bytes)
- **raw transcripts (this run)** — `/tmp/v2-rwt/v2-chat-results.json` (104,610 bytes)
- **llama-server log** — `/tmp/v2-rwt/v2-chat-server.log` (72,056 bytes)
- **v2 ChatML baseline transcripts** — `/tmp/v2-rwt/v2-results.json` (64,261 bytes)
- **v1 ChatML baseline transcripts** — `/tmp/v2-rwt/v1-results.json` (93,971 bytes)
