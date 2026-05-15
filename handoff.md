# Handoff — agent-memory

## Current Status (2026-05-13, late session)

### V2 fine-tune data pipeline COMPLETE — ready for training

All 7 sub-issues of v2 parent #25 closed. Branch `feat/v2-finetune-data-pipeline` has:

| # | Title | PR | Status |
|---|---|---|---|
| #26 | Anti-loop inference guard (`--anti-loop` flag) | #34 | merged |
| #27 | Migration 012 — prompt↔tool_call linkage schema | #35 | merged |
| #36 | Project consolidation (git root + remote + branch) | #37 | merged |
| #30 | Live prompt capture via UserPromptSubmit hook | #38 | merged |
| (ops) | Daily DB backups via launchd (idempotent install) | #39 | merged |
| #28 | Backfill tool_calls + prompts from Claude jsonl | #40, #41 | merged |
| #32 | `build_v2_dataset.py` — Qwen 2.5 chat-format dataset | #42 | merged |

**Remaining for v2 ship:**
- **#33 — Retrain v2** (reuses PR #24 pipeline; 1 epoch, tool descriptions). Open and unblocked.
- **#31 — Recall surface (`tool_calls[]` on `/api/observations`)** — out of v2-training critical path; runtime quality improvement, can land later.

### V2 data ready

```
data/processed/qwen25_tools/v2/
  train.chat.jsonl      23,983 rows (real prompts, real tool args, real responses)
  valid.chat.jsonl       1,588 rows (5% session-aware split)
  train.tiny.jsonl         200 rows (deterministic, seed=42)
  valid.tiny.jsonl          30 rows
  tool_schemas.json     35 schemas (22 from v1 + 13 recovered: Agent + MCP tools)
  MANIFEST.json         drop reasons, tool histogram, output hashes
```

100/100 random rows render cleanly through `tokenizer.apply_chat_template(...)`.

### V2 vs V1 — the key fix

**v1:** `"Call tool 'Bash' with appropriate arguments."` × 16,944 rows → model never learned to commit to argument content from a real prompt → empty-args inference loops in production.

**v2:** Real user prompts joined to real tool_calls. Every row's user message is an actual prompt from a Claude Code session. 0% synthetic.

Sample v2 row:
```
[USER]  ok can you go into research folder and create a reference repo …
[ASST]  tool_call Grep({"glob": "anvil/tui/*.py",
                       "pattern": "BINDINGS|check_action|active_bindings",
                       "output_mode": "content"})
[TOOL]  Grep: anvil/tui/interactive_enhanced.py:89: BINDINGS = [...]
```

### Live DB state (as of merge of #42)

| Table | Rows | Notes |
|---|---|---|
| `mem_tool_calls` (backfill_jsonl) | 28,599 | 100% linked to a user prompt |
| `mem_tool_calls` (live) | 55,303 | historical, `prev_user_prompt_id = NULL` |
| `mem_user_prompts` | 4,502 | (+3,092 from #28 backfill) |
| `mem_sessions` | 1,062 | |
| `mem_projects` (canonical, source_kind='git') | ~120 | post-#36 consolidation |

DB backup file before any v2 work: `data/backups/pre_v2_backfill_20260513_211653.sql.gz` (319 MB).
Daily backups run at 03:14 local; retention is the most recent 3 `daily_*.sql.gz`.

---

## Next Session — Start Here

**You are on branch `feat/v2-finetune-data-pipeline` off main.** All data work is done. The remaining task is **#33: train the v2 model**.

### Sanity checks before training

```bash
git status                                             # clean, on the feat branch
gh pr view 42 --repo metazen11/agent-memory            # MERGED (3628be8)
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_tool_calls WHERE retention_class='backfill_jsonl' AND prev_user_prompt_id IS NOT NULL;"
# Expect: 28,599  (100% linked)
ls data/processed/qwen25_tools/v2/                     # 6 files including MANIFEST.json
wc -l data/processed/qwen25_tools/v2/train.chat.jsonl  # 23,983
.venv-finetune/bin/python -m pytest tests/fine_tune/ -q  # 64 passing (existing + #32 new)
```

### Run v2 training (issue #33)

The pipeline from PR #24 is the recipe. Phase-gated, runbook at `docs/fine_tune/PIPELINE_RUNBOOK.md`.
Full execution plan with rollback per phase: `docs/fine_tune/V2_TRAINING_PLAN.md`.

**IMPORTANT:** `run_train_lora.py` is **env-var driven**, not argparse. The
script reads `MODEL_SLUG`, `DATASET_VERSION`, `DATASET_TIER`, `RUN_TAG`,
`EPOCHS`, etc. from the environment.

```bash
# 1. Pre-flight — Dropbox is the cold-storage symlink target; must quit
#    so it doesn't move files mid-training.
osascript -e 'tell application "Dropbox" to quit'

# 2. Phase 1-2 (base model already downloaded for v1; reused for v2).
# 3. Phase 3 — restructure (not needed for v2; build_v2_dataset.py already
#    emits the final chat-template shape).

# 4. Phase 4 — TINY training run (200 rows, 1 epoch). Catches dataset
#    bugs in ~25-40 min before the full run.
DATASET_TIER=tiny DATASET_VERSION=v2 RUN_TAG=v2-tiny-smoke \
  .venv-finetune/bin/python -u models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py

# 5. Phase 4 validator — must pass ≥ 3% parse rate on the tiny set.
#    (Run AFTER merging the LoRA adapter and converting to GGUF — see
#    V2_TRAINING_PLAN.md Phase 3-4 for the full sequence.)
.venv-finetune/bin/python scripts/fine_tune/validate_tool_calls.py \
    --backend llama-cli \
    --gguf models/gguf/qwen2.5-3b-toolcalls-v2-tiny-q4km.gguf \
    --min-parse-rate 0.03 \
    --anti-loop --model-version v2-tiny

# 6. Phase 5 — FULL training. ~3-4h wall clock on M-series MPS.
DATASET_TIER=full DATASET_VERSION=v2 RUN_TAG=v2-full \
  .venv-finetune/bin/python -u models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py

# 7. Phase 5 validator — must pass ≥ 85% on merged HF + GGUF backends.
# 8. Phase 6 — GGUF convert + LM Studio install.
# 9. Phase 7 (NEW) — chat-loop verification via llama-server on the v2 GGUF.
#    Restart Dropbox ONLY after both LM Studio AND chat-loop pass.
```

**LoRA output dir** is `models/lora/qwen2.5-3b-instruct-toolcalls-lora/`
(with `-instruct-`), not `qwen2.5-3b-toolcalls-lora/` (that's where the
training script lives). The `latest` symlink in the output dir advances
on successful training completion.

### Critical reminders before training

- **Quit Dropbox** before any new training run (`osascript -e 'tell application "Dropbox" to quit'`). `models/` is a symlink into Dropbox cold storage; sync mid-training corrupts checkpoints.
- **Use `.absolute()` not `.resolve()`** for paths under `models/` (avoids symlink chase into Dropbox; documented in `docs/fine_tune/FAILURE_MODES.md` #1).
- **Don't overwrite v1 GGUF.** v1 is the current shipped artifact at `models/gguf/qwen2.5-3b-toolcalls-q4km.gguf`. v2 goes to `models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf`.
- **24 pytest tests at `tests/fine_tune/` must keep passing.** Plus 28 new from #32. Run `.venv-finetune/bin/python -m pytest tests/fine_tune/ -q` before merging.
- **Two `gh` accounts.** `wfca-mz` is read-only on `metazen11/agent-memory`; `metazen11` has push perms. Switch with `gh auth switch --user metazen11` before push/merge.

### Eval set for the loop bug

`tests/fine_tune/fixtures/vague_prompts.txt` — 50 vague natural prompts ("find the fire-map codebase", "what's broken in the build"). v2 must emit **0 empty-args loops** on these (the v1 failure mode). Anti-loop guard (`scripts/fine_tune/validate_tool_calls.py --anti-loop`) is the canary.

### Anchor docs

- `docs/fine_tune/V2_DATA_PIPELINE_PLAN.md` — full plan with per-step acceptance criteria + quality-gate review.
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — phase-by-phase training recipe.
- `docs/fine_tune/FAILURE_MODES.md` — 11 known failures + fixes (incl. v1 empty-args loop, anti-loop mitigation).
- `docs/backups.md` — daily backup operator reference.

---

## Previous Status (2026-05-13, morning)

### Fine-tune v1 SHIPPED — PR #24 merged to main
- Qwen 2.5-3B tool-call LoRA, 80% validator pass rate, 100% on natural prompts.
- PR: https://github.com/metazen11/agent-memory/pull/24 (squash-merged 2026-05-13T21:55:07Z)
- Closes issues #15–#22; leaves #23 open (LM Studio manual verification).
- GGUF at `models/gguf/qwen2.5-3b-toolcalls-q4km.gguf` (SHA `5e174a04…`), 1.8GB Q4_K_M.
- Loaded in LM Studio at `~/.lmstudio/models/mz/qwen2.5-3b-toolcalls/` (use `~/.lmstudio/...` NOT `~/.cache/lm-studio/...`).

### Loop bug discovered in v1 — drove the v2 plan
When given vague natural prompts in LM Studio ("find the fire-map codebase"), v1 model emits `<tool_call>` blocks with **empty `arguments`**, gets a generic result back, repeats. Infinite loop until context fills.

Root cause: **83 % of v1 training prompts were synthetic** (`"Call tool 'X' with appropriate arguments"`). The model never had to commit to argument content from a real user prompt. Only 17 % (2,682 rows) came from real Claude conversations.

### LM Studio MCP config
Updated `~/.lmstudio/mcp.json`: anvil + agent-memory entries now point at `/Users/mz/_CODING/...` (was Dropbox). **Restart LM Studio** to pick up.

### Fire-map not in Anvil's workspace index
`~/.anvil/workspace_index.json` (generated 2026-05-12) does not include the `_CODING/fire-map.wfca.com/` tree even though it's under the workspace root. Needs `anvil workspace reindex` or explicit root add — Anvil-side configuration, not agent-memory.

---

## Previous Status (2026-05-12)

### Location Change
- **Moved from** `~/Dropbox/_CODING/agentMemory/` **to** `~/_CODING/agentMemory/`
- `models/` and `.venv-finetune/` are symlinks back to Dropbox (79GB cold storage)
- Claude hook symlinks at `~/.claude/hooks/agent-memory-*.js` updated to new path
- Anvil `.mcp.json` updated to new path

### Security Sprint (issues #1-#14)
Shipped in 8 commits to main. Key changes:

**Auth system:**
- `REQUIRE_AUTH=true` in `.env` enables Bearer token auth on all endpoints
- `TRUSTED_AGENTS=anvil,claude,codex,gemini,python-httpx` bypasses auth for known localhost callers
- Hooks send `X-Agent-Name: claude` header for trusted bypass
- Token CLI: `python -m app.cli setup` generates tokens for default agents

**Currently auth is ON** (`REQUIRE_AUTH=true`) with trusted agents bypass active.

**Other security:**
- Host bound to `127.0.0.1` (was `0.0.0.0`)
- `trust_remote_code` removed from embeddings (configurable via `EMBEDDING_TRUST_REMOTE_CODE`)
- CORS middleware locked to localhost origins
- Rate limiting enabled (100 writes/min, 500 reads/min)
- Audit logging enabled (writes_only mode)
- Secret redaction enabled by default (`REDACT_SECRETS=true`)
- PG trust auth warning on startup

### Known Issue — PreToolUse Hook Error in Claude
The `pre-tool-use.js` hook may show errors in Claude sessions started before the auth changes. **Fix: restart Claude Code session** so the updated hook code and env vars load.

## Daily backups

Daily `pg_dump` at 03:14 local time via launchd. Retains the 3 most recent
`data/backups/daily_*.sql.gz` files. Manual snapshots (e.g.
`pre_v2_backfill_*.sql.gz`) are preserved.

`hooks/ensure-services.js` calls `ensureBackupSchedule()` on session start
to (idempotently) install the job on macOS. See `docs/backups.md`.

Verify:
```bash
bash scripts/install_backup_schedule.sh --check
```

## Setup on New Machine

```bash
# 1. Clone/pull
cd ~/_CODING/agentMemory && git pull

# 2. Install deps
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Run migrations (auto on startup)
.venv/bin/uvicorn app.main:app --port 3377 --host 127.0.0.1

# 4. Generate tokens
.venv/bin/python -m app.cli setup

# 5. Set env
echo 'REQUIRE_AUTH=true' >> .env
echo 'export AGENT_MEMORY_TOKEN="<claude-token-from-step-4>"' >> ~/.zshenv

# 6. Update Claude hook symlinks
cd ~/.claude/hooks
ln -sf ~/_CODING/agentMemory/hooks/session-start.js agent-memory-session-start.js
ln -sf ~/_CODING/agentMemory/hooks/session-end.js agent-memory-session-end.js
ln -sf ~/_CODING/agentMemory/hooks/pre-tool-use.js agent-memory-pre-tool-use.js
ln -sf ~/_CODING/agentMemory/hooks/post-tool-use.js agent-memory-post-tool-use.js
ln -sf ~/_CODING/agentMemory/hooks/ensure-services.js agent-memory-ensure-services.js
ln -sf ~/_CODING/agentMemory/hooks/user-prompt-submit.js agent-memory-user-prompt-submit.js

# 7. Register UserPromptSubmit hook in ~/.claude/settings.json — see
#    docs/fine_tune/V2_DATA_PIPELINE_PLAN.md or the example in
#    hooks/user-prompt-submit.js docstring.

# 8. Install daily backup schedule
bash scripts/install_backup_schedule.sh
```

## Feature Toggles

- `AGENT_MEMORY_HINTS_ENABLED` (global default)
- `AGENT_MEMORY_SESSION_HINTS_ENABLED` (session-start hints)
- `AGENT_MEMORY_PRE_TOOL_HINTS_ENABLED` (pre-tool warnings)
- `AGENT_MEMORY_RECALL_SHAPE` (planned for #31; default `v1` until that ships)
- `AGENT_MEMORY_MCP_EXPOSE_TOOL_INPUT` (planned for #31; default OFF)
- Terminal toggle interface: `node scripts/hints-config.js status|set|tui`

## Resume Commands

### Service & Auth

```bash
# Check service health
curl http://localhost:3377/api/health

# List tokens
python -m app.cli list-tokens

# Search prompts
curl -H 'X-Agent-Name: claude' 'http://localhost:3377/api/prompts/search' \
  -H 'Content-Type: application/json' -d '{"query":"auth","limit":5}'

# Check toggle state
node scripts/hints-config.js status

# Backup status
bash scripts/install_backup_schedule.sh --check
ls -lht data/backups/daily_*.sql.gz | head -3
```

### v2 Dataset

```bash
# Rebuild from current DB state (idempotent — overwrites v2/ output dir)
.venv/bin/python scripts/fine_tune/build_v2_dataset.py --write

# Inspect a row
head -1 data/processed/qwen25_tools/v2/train.chat.jsonl | python3 -m json.tool

# Verify chat template renders
.venv-finetune/bin/python -c "
import json
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained('models/base/qwen2.5-3b-instruct',
    local_files_only=True, trust_remote_code=False)
with open('data/processed/qwen25_tools/v2/train.chat.jsonl') as f:
    row = json.loads(f.readline())
print(tok.apply_chat_template(row['messages'], tools=row['tools'], tokenize=False)[:500])
"
```

### Training

Reading order:
- `AGENTS.md` — file map.
- `docs/fine_tune/V2_DATA_PIPELINE_PLAN.md` — what's done, what's next.
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — phase-by-phase training recipe.
- `docs/fine_tune/FAILURE_MODES.md` — 11 known failures + fixes.
- `docs/fine_tune/LMSTUDIO_INTEGRATION.md` — LM Studio gotchas.

Historical (deprecated): `docs/training_notes.md` — Qwen 3.5 9B and Gemma 4 paths both failed.
