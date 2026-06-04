# AGENTS.md — agent-memory

Operating guide for agents (Claude Code, Anvil, Codex, etc.) working in this
repository. Read this before editing.

## What this repo is

A local-first memory store + API for agent conversations. FastAPI app on
port 3377, backed by Postgres, with embedding-based search. Plus a
fine-tuning pipeline for tool-calling models (see below).

For the broader project context, read `handoff.md`.

## Memory store + API

Installs as a Claude Code plugin (see `.claude-plugin/plugin.json`).
The plugin contract:

- **Skills** (`./skills/`) — `search`, `timeline`, `get_observations`,
  `save_memory`, `create_lesson`, `search_lessons`, `memory_search_guide`,
  `mem-search`. Three-layer search workflow: `search` returns IDs only,
  `timeline` gives context around an anchor, `get_observations` hydrates
  full details.
- **MCP server** (`./.mcp.json` → `scripts/run_mcp.sh` → `mcp_server.py`).
  Tools include `search`, `recall` (one-call search + hydrate), `save_memory`,
  `create_lesson`, `abilities_memory` (lazy operator manual — keeps
  session-start preamble tiny). Pass `project="<cwd>"` on every call.

  **Do not add `enabledMcpjsonServers: ["agent-memory"]` or
  `enableAllProjectMcpServers: true` to this repo's `.claude/settings.local.json`.**
  `.mcp.json` is the plugin manifest (read by the plugin loader, which sets
  `CLAUDE_PLUGIN_ROOT`). When project-scope MCP auto-loading is also enabled
  Claude tries to launch the same file as a project MCP, but project scope
  does not set `CLAUDE_PLUGIN_ROOT`, so the launch fails and a duplicate
  `agent-memory ✗ Failed to connect` entry appears alongside the working
  plugin entry. See `docs/adr/0001-claude-mcp-single-source-of-truth.md`.
- **Hooks** (`./hooks/hooks.json`) — `SessionStart` (ensures services up,
  injects compact preamble), `UserPromptSubmit` (injects active CRITICAL
  lessons), `PreToolUse` (matches lessons against tool input; one-time
  empty-result reminder), `PostToolUse` (queues observations), `Stop`
  (session end). Hook scripts resolve sibling modules via
  `fs.realpathSync(__filename)` so they survive symlink invocation.

HTTP surface (FastAPI app on `:3377`):

- `/api/queue` — accept tool call data. Path-normalizes Dropbox→local;
  does NOT redact secrets (redaction happens at search/export boundary).
- `/api/observations`, `/api/observations/search`, `/api/recall` —
  read-side search and hydration.
- `/api/lessons`, `/api/lessons/match` — proactive rule system. `/match`
  uses strict one-directional path scoping (`project_path_filter_strict`
  in `app/project.py`) so a parent cwd does NOT pull child-project
  lessons.
- `/api/tool-calls`, `/api/tool-calls/export`, `/api/tool-calls/export/dataset` —
  fine-tune dataset export. Both export paths run `redact_text` /
  `redact_json` over `tool_input`, `tool_response_preview`, `prompt_text`,
  and `tool_error` before writing. Regression tests in
  `tests/test_api_tool_calls.py` pin this contract.
- `/api/integration_guide` — self-describing integration recipes for
  embedding agent-memory into other hosts. Also rendered at
  `docs/INTEGRATION.md`.

Redaction patterns live in `app/redact.py` (`SECRET_PATTERNS` + optional
`PII_PATTERNS`). When adding a new secret pattern, no other change is
required — the export pipeline picks it up automatically because
`_base_record` in `app/dataset_exports.py` is the single bottleneck.

Migrations live in `scripts/migrations/` (numeric prefix). Migration 015
quarantined super-project rows (e.g. `/`, `/Users/mz`) that were leaking
lessons cross-project via the bidirectional prefix filter.

Database access from shell or tests: always use `scripts/psql_wrapper.sh`
— never inline `PGPASSWORD=...`. The wrapper reads `DATABASE_URL` from
`.env` and keeps the password out of process listings and shell history.

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
