# AGENTS.md — agent-memory

Operating guide for agents (Claude Code, Anvil, Codex, etc.) working in this
repository. Read this before editing.

## What this repo is

A local-first memory store + API for agent conversations. FastAPI app on
port 3377, backed by Postgres, with embedding-based search. Plus a
fine-tuning pipeline for tool-calling models (see below).

For the broader project context, read `handoff.md`.

## Fine-tuning

End-to-end pipeline for LoRA fine-tuning a Qwen 2.5 / Qwen 3 / similar
instruct model on the project's own tool-call data, exported as a GGUF
usable in LM Studio.

Reading order:

1. **`docs/fine_tune/PIPELINE_RUNBOOK.md`** — the phase-by-phase recipe.
2. **`docs/fine_tune/FAILURE_MODES.md`** — known issues + fixes; check here
   first if anything breaks.
3. **`docs/training_notes.md`** — historical notes; deprecated paths called
   out (Qwen 3.5 9B and Gemma 4 paths both failed).

Reusable code lives in:

- `scripts/fine_tune/lib.py` — canonical paths, model registry, hashing,
  logging helpers. Add a new model entry to `MODELS` here to bring it
  under the pipeline.
- `scripts/fine_tune/download_base.py` — HF snapshot download + REVISION
  pinning.
- `scripts/fine_tune/smoke_test_base.py` — load + 8-token generation check.
- `scripts/fine_tune/validate_tool_calls.py` — Hermes-format tool-call
  validator. Backends: `llama-cli` for local GGUFs, `openai` for LM Studio
  / Ollama / vLLM servers.
- `scripts/fine_tune/lmstudio_smoke.sh` — copies GGUF into LM Studio's
  models dir and runs the openai-backend validator.
- `fine-tune/restructure_to_qwen_tools.py` — converts the raw chat-format
  dataset into Qwen 2.5 native tool-call structure (`tools=` + `tool_calls`
  / `role: "tool"` messages). Includes PII scrub + JSON-schema inference.
- `models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py` — the training
  script. Reusable across models via `MODEL_SLUG` env var.
- `tests/fine_tune/test_validator.py` — pytest unit tests for the
  tool-call parser.

Run all fine-tune tests:

```bash
.venv-finetune/bin/python -m pytest tests/fine_tune/ -v
```

## Operating model

- Track work via `TaskCreate` / `TaskUpdate`. Mark tasks `in_progress` when
  starting, `completed` immediately when done.
- Log every long-running step to `logs/m-ft-1/` (or its phase equivalent)
  with UTC timestamps.
- Don't `.resolve()` paths under `models/` — that chases the Dropbox
  symlink. Use `.absolute()` or untouched relative paths.
- Don't run training while Dropbox is syncing — quit it first.
