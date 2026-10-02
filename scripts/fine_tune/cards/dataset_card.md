---
license: other
tags:
  - tool-calling
  - qwen3
  - agent-memory
  - internal
pretty_name: agent-memory tool-call v5 pilot
---

# agentmem-toolcalls-v5-pilot (PRIVATE / INTERNAL)

> **INTERNAL — DO NOT MAKE PUBLIC.** This dataset contains sensitive internal
> content (internal paths, project names, and organization references). It is
> private by policy and MUST remain private. Do not mirror, export, or share.

## What this is

The **v5 pilot** training dataset for the agent-memory tool-call model — 5000
rows of conditioned, redacted tool-call conversations derived from real
agent-memory tool-call traces. Used to fine-tune a LoRA adapter on
`Qwen/Qwen3-4B` (see `WFCA/qwen3-4b-toolcalls-v5-pilot`).

## Format

One JSON object per line (`train.jsonl`). Keys:

| Key | Type | Meaning |
|---|---|---|
| `messages` | list | Chat turns (`system` / `user` / `assistant` / `tool`). The assistant turns contain the tool calls to be learned. |
| `tools` | list | Tool schema(s) available for that conversation (passed to `apply_chat_template(tools=...)`). |
| `project_tag` | str | Project the trace came from (used for project conditioning). |
| `multi_turn` | bool | Whether the conversation spans multiple tool-call turns. |
| `source_ids` | list | Provenance IDs back to the source traces. |

## Provenance & conditioning

Rows are pulled from the agent-memory database through the sanctioned DB path,
**redacted** (secrets stripped; absolute paths normalized to a trusted root),
and **project-conditioned** so the model learns to attend to the active project
context. Build + audit tooling: `scripts/fine_tune/build_v5_pilot_dataset.py`
and the accompanying `AUDIT.md`.

## Intended use

Fine-tuning tool-call models for the agent-memory assistant harness. Not a
general-purpose corpus.

## Stats

- Rows: 5000
- Split: `train`
- Redaction spot-check: 0 secret-pattern matches (count-only check at build time).
