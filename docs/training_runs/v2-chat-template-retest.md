# v2 chat-template retest — was the harness the problem?

**Date:** 2026-05-15
**Model under test:** `models/gguf/qwen2.5-3b-toolcalls-v2-q6k.gguf` (Q6_K)
**Server:** `llama-server --jinja -c 8192` on `127.0.0.1:9099`
**Endpoint:** `POST /v1/chat/completions` (OpenAI-compatible)
**Harness:** `/tmp/v2-rwt/harness_chat.py`
**Raw results:** `/tmp/v2-rwt/v2-chat-results.json`
**Server log:** `/tmp/v2-rwt/v2-chat-server.log`

## Why this retest exists

The previous A/B (`docs/training_runs/v2-real-world-test.md`) showed v2 strictly worse than v1: 0/10 useful answers (vs 3/10), 9/10 loop rate (vs 4/10), 3/10 multi-turn adaptation (vs 9/10). Before retracting v2 we wanted to rule out that the regression was a harness artifact: the hand-rolled ChatML wrapped tool results as `<|im_start|>user\n<tool_response>...</tool_response><|im_end|>`, which might not match the chat template the model was actually trained against.

## Setup

- Booted v2 Q6_K under `llama-server --jinja` (model's own Jinja template used)
- Inspected `/props.chat_template` — present in GGUF metadata, full template loaded; `--jinja` enabled by default in this llama.cpp build
- Sent 10 identical prompts via `/v1/chat/completions` with OpenAI `tools` array, `tool_choice: "auto"`, OpenAI `tool` role for results (`{"role":"tool","tool_call_id":...,"content":...}`)
- Reused `PROMPTS`, schemas, `fake_tool_result()`, `detect_loop()`, and `analyze_session()` from `tests/fine_tune/real_world/harness.py` via import (cannot drift)

## Important harness-format finding (not the bug)

The v2 GGUF's actual Jinja template renders tool messages as:

```
<|im_start|>user
<tool_response>
{content}
</tool_response><|im_end|>
```

This is **exactly** what the original hand-rolled ChatML harness produced. So at the rendered-prompt level, both harnesses feed the model nearly identical text. The chat-completions API adds proper structured tool_calls parsing on the response side, which is the real win, but the prompt the model sees is the same shape.

## Per-prompt outcome comparison

| # | Prompt (truncated) | v1 (ChatML) | v2 (ChatML) | v2 (chat-completions + --jinja) |
|---|---|---|---|---|
| 1 | validator system prompt build site | gave_up | looped | looped |
| 2 | fine_tune changes last week | useful_answer | looped | looped |
| 3 | empty-args loop fix test | gave_up | looped | looped |
| 4 | memory leak in MCP server | looped | looped | looped |
| 5 | summary of all GGUF files | looped | looped | looped |
| 6 | TODO comments grouped by file | gave_up | text_only_fallback | gave_up |
| 7 | v1 vs v2 training data diff | useful_answer | looped | looped |
| 8 | training row path raw→dataset | looped | looped | looped |
| 9 | most recent commit | useful_answer | looped | looped |
| 10 | how to run validator | looped | looped | looped |

## Aggregate stats

| metric | v1 ChatML | v2 ChatML | v2 chat-comp + --jinja |
|---|---|---|---|
| useful_answer | 3/10 | 0/10 | **0/10** |
| looped | 4/10 | 9/10 | **9/10** |
| gave_up | 3/10 | 0/10 | 1/10 |
| text_only_fallback | 0/10 | 1/10 | 0/10 |
| multi_turn_adapted | 9/10 | 3/10 | **8/10** |
| turn-1 args populated | 10/10 | 9/10 | 10/10 |

## What changed under the chat-completions harness

- **Multi-turn adaptation went up** (3→8/10): with the OpenAI tool role, the model does often pick a *different* call on subsequent turns rather than emitting the literal same `<tool_call>` JSON. This is the behavior the v2 trained-against format encourages.
- **Useful-answer rate did not move** (0→0/10): the model never synthesizes a final text reply. After each tool result it just emits another tool_call. It keeps tool-calling until it loops (3 identical / 3 empty-args calls) or hits max_turns.
- **Another failure mode showed up:** on 5/10 prompts the model produced a tool_call whose JSON arguments string was so long it hit `max_tokens=512` mid-string, leaving it un-parseable (harness preserves the truncated text as `arguments._raw`). Several of these were `python3.11 -c "..."` one-liners with hundreds of lines of duplicated `print(...)` statements — a generative pathology where the model gets stuck in a within-args repetition loop. v1 never produced this.
- **Args quality**: turn-1 args were always populated; this is consistent with the v2 fine-tune fixing the empty-args bug from v1. That part of v2 did work.

## Failure pattern (representative)

Prompt 9 ("Show me the most recent commit and what it changed."):

```
turn 1: Bash(command="git log --oneline -3")  -> fake stdout returned
turn 2: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  -> fake content
turn 3: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  [same args]
turn 4: Read(file_path="/Users/mz/Dropbox/_CODING/anvil/HANDOFF.md")  [same args] -> looped
```

The model had everything it needed at turn 1's tool result to answer in plain text. Instead it asked for a second file, then re-asked for the same file twice. This is a synthesis failure, not a format issue.

## Errors / template issues encountered

None. `--jinja` was accepted, the GGUF's embedded chat_template loaded cleanly, `tools` were accepted, `finish_reason="tool_calls"` was returned correctly on tool-emitting turns, and the OpenAI `tool` role messages were accepted without complaint. No silent fallback occurred.

## Verdict

**The harness was not the problem.** When v2 is run through the model's own Jinja chat template via `/v1/chat/completions`, useful_answer rate stays at 0/10 and loop rate stays at 9/10. The multi_turn_adaptation count rose (3→8) because the OpenAI tool role does coax the model into varying its calls, but adaptation doesn't translate into final text answers — the model never decides "I have enough; respond in prose."

**v2 is genuinely worse than v1 at the agentic task.** v2 emits well-formed first-turn calls (empty-args bug fixed, args populated 10/10) but cannot terminate a tool loop and does not synthesize from tool results. v1 produced 3 useful answers; v2 produces 0 under either harness.

A new failure mode also showed up: 5/10 prompts had max_tokens-truncated argument JSON with repeated lines, suggesting v2 has degraded generation quality inside `arguments` strings on top of the synthesis failure.

## Recommendation

**Retraction stands.** Do not ship v2 as a replacement for v1. The hypothesis that v2's regression was a ChatML-format artifact is rejected by direct measurement.

Next steps for a v3 should target:
1. The "never stops tool-calling" failure — likely a training-data imbalance: too few examples that end in a synthesized text answer after tool results, too many that end with another tool_call.
2. The in-args repetition failure — likely insufficient regularization or a data leak where long shell scripts were over-represented in `arguments` strings.

## Artifacts

- `/tmp/v2-rwt/harness_chat.py` — chat-completions harness
- `/tmp/v2-rwt/v2-chat-results.json` — raw transcripts (10 sessions, full per-turn detail)
- `/tmp/v2-rwt/v2-chat-server.log` — llama-server log incl. loaded chat_template
- `/tmp/v2-rwt/v2-results.json` — prior hand-rolled ChatML transcripts (reference)
- `/tmp/v2-rwt/v1-results.json` — prior v1 transcripts (reference)
