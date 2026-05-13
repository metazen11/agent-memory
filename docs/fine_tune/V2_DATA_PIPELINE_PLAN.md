# V2 Fine-Tune Data Pipeline Plan

**Branch:** `feat/v2-finetune-data-pipeline`
**Parent issue:** #25
**Created:** 2026-05-13
**Status:** Plan revised post quality-gate review (verdict: `approve_with_changes`).
All blocker findings addressed in-place. Review JSON at
`docs/fine_tune/reviews/25-quality-gate.json`.

**Pinned environment:**
- PostgreSQL 16.13 (Homebrew). All migration patterns assume PG ≥ 12.
- Python 3.x with `.venv-finetune/`.
- Node hooks at `hooks/*.js`.

---

## Why v2

V1 (PR #24, merged) hit 80 % validator pass rate but exhibits an **empty-args
infinite-loop bug** in LM Studio on vague natural prompts. Root cause: 83 % of
v1 training prompts were synthetic (`"Call tool 'X' with appropriate
arguments"`) because the original export couldn't join tool calls back to the
user prompt that originated them.

The data linkage gap is in agent-memory itself:

| Table | Rows | Status |
|---|---|---|
| `mem_tool_calls` | 54,987 | ✅ Full `tool_input` JSON, response, success/error |
| `mem_user_prompts` | 1,410 | ❌ Only 54 of 893 sessions captured prompts |
| `mem_sessions` | 893 | Most lack matching user_prompts |
| `mem_projects` | 686 | OK, but Dropbox/local path forks not deduped |

Zero `mem_tool_calls` rows can be joined to a same-session user prompt by FK.

Source of truth — `~/.claude/projects/**/*.jsonl` — has the full turn history
(user msg → tool_use blocks → tool_result blocks) for every coding session.
Estimated 50–100k tool calls with linked prompts available for backfill.

## Goals

1. Close the prompt → tool-call linkage gap (backfill + fix live capture).
2. Surface `tool_input` on the recall path so agents at runtime see actual
   arguments, not just narratives.
3. Build a v2 dataset of ≥ 25k multi-turn rows with real user prompts.
4. Retrain v2 with anti-loop inference guard. Target: 0 empty-args emissions
   on 50-prompt LM Studio eval set.

## Non-goals

- Re-architecting the schema beyond targeted additions.
- Retiring v1; v1 GGUF stays as the shipped artifact until v2 passes eval.
- Web UI work (issue #11) — separate track.
- Splitting `app/main.py` into `app/api/`. Plan extends in place.

## Key decisions (logged for audit)

- **Retention:** default data retention is **365 days** for both `live`
  and `backfill_jsonl` classes, measured from the **original jsonl turn
  timestamp** (the `created_at` recorded at insert, not import time).
  Backfilled rows carry `retention_class = 'backfill_jsonl'` for audit
  traceability. Existing `audit_retention_days = 30` in `app/config.py:46`
  becomes a separate **audit-log** retention; data retention is new
  `data_retention_days = 365`. Issue #10 lands the formal policy + purge job.
  - **Current corpus check (2026-05-13):** all 2,372 jsonl files are
    ≤ 180 days old. No data loss from this rule today.
  - **Future caveat:** later imports of older archives will be purged on
    import if older than 365 d. Document in `FAILURE_MODES.md`; consider
    bumping to 730 d before any historical-archive import.
- **Handler location:** `/api/observations` stays in `app/main.py`.
- **MCP exposure:** `tool_input` on MCP `search` is gated by
  `AGENT_MEMORY_MCP_EXPOSE_TOOL_INPUT` (default OFF).
- **Recall scope:** stays inside #25; load-bearing for verifying backfill.
- **Old jsonl:** import everything regardless of age (tagged for retention).
- **Tool-cap strategy:** Bash is the shell envelope; cap by the
  **first-non-flag-token of `command`** (the actual program: `git`, `pytest`,
  `psql`, `gh`, etc.), not by the literal tool name.

---

## Acceptance criteria (parent / overall)

- [ ] `mem_user_prompts` ≥ 30k rows post-backfill (from 1,410).
- [ ] ≥ 80 % of `mem_tool_calls` rows **whose session has at least one
  `mem_user_prompts` row** have a same-session `prev_user_prompt_id`,
  measured immediately after #28's `--commit`.
- [ ] Live capture: ≥ 95 % of `mem_tool_calls` rows with `created_at` after
  #29 merge have `prev_user_prompt_id` set.
- [ ] `/api/observations` and (opt-in) `mcp__agent-memory__search` responses
  include `tool_calls[].{name, input, response_preview, created_at}`.
- [ ] `data/processed/qwen25_tools/v2/` contains ≥ 25k multi-turn rows; ≥ 90 %
  derive from real user prompts (sentinel-tagged).
- [ ] V2 GGUF passes existing 24 pytest tests + a new 50-prompt empty-args
  eval with 0 loops.
- [ ] `PIPELINE_RUNBOOK.md` and `FAILURE_MODES.md` updated.
- [ ] Observability surface (see below) live and queryable.

## Observability (parent-level)

Five named metrics, owned per-step, queryable via `/api/stats`:

| Metric | Owner step | Alert |
|---|---|---|
| `linkage_ratio_24h` (% of new tool_calls with prev_user_prompt_id) | #29 | < 0.5 for 1h |
| `observation_recall_p99_latency_ms` | #30 | > 500 ms |
| `redaction_miss_in_recall_count` | #30 | > 0 |
| `backfill_progress_pct` (during run) | #28 | dashboard only |
| `hook_error_rate` | #29 | > 1 % for 5 m |
| `empty_args_emissions_total{model=v1|v2}` | #32 | > 1 per 1000 calls |

## Risks

- **Schema migration on populated DB.** Mitigate: `CREATE INDEX CONCURRENTLY`
  outside transaction; FK uses `NOT VALID` + later `VALIDATE CONSTRAINT`.
- **Backfill idempotency.** Mitigate: per-session atomic transaction;
  re-runs detect partial sessions via `(session_id, turn_index, role,
  content_hash)` and complete-or-skip them.
- **PII in jsonl.** Mitigate: recursive `redact_json()` walks every string
  leaf of `tool_input` and `prompt_text` before INSERT; second-pass scan at
  recall-response-build time emits warn-level log on `[REDACTED:*]`-missing
  rows that pattern-match a known secret.
- **Loop bug regression.** Mitigate: anti-loop inference guard + production
  metric `empty_args_emissions_total` tagged by model version.
- **Recall surface exposes raw args.** Mitigate: MCP exposure gated by
  default-OFF env var; HTTP `/api/observations` inherits existing auth tier
  (verify in test).

## Rollback

Per-step commits on this branch. Detailed rollback per step:

- **Schema (#26):** migration reversal SQL committed alongside as
  `app/migrations/012_v2_data_pipeline.down.sql`. New columns are nullable;
  dropping is non-destructive.
- **Backfill (#27/#28):** every inserted row stamped with
  `backfill_run_id = <run-start UTC>`. Rollback:
  ```
  DELETE FROM mem_user_prompts WHERE backfill_run_id = '<id>';
  UPDATE mem_tool_calls SET prev_user_prompt_id = NULL, backfill_run_id = NULL
    WHERE backfill_run_id = '<id>';
  ```
  Observation-generation queue is **bypassed** during backfill — backfilled
  rows are training-data-only, not runtime memory.
- **Hooks (#29):** prior version saved to `hooks/legacy/<name>.pre-v2.js`;
  `HOOKS_LEGACY=1` env var forces revert without redeploy.
- **Recall surface (#30):** env-flagged via `AGENT_MEMORY_RECALL_SHAPE`
  (`v1` | `v2`, default `v1` until #28 lands). Tests flip the flag.
  Revert = unset env var.
- **V2 model (#32):** v1 GGUF stays untouched in `models/gguf/`. LM Studio
  swap: rename the v2 symlink, restart LM Studio. `HOOKS_FORCE_V1_MODEL=1`
  env var forces hooks to keep using v1 even if v2 is loaded.

---

## Step 1 — Schema additions

**Issue:** #26

**Migration 012** at `app/migrations/012_v2_data_pipeline.sql` + reversal
`.down.sql`. Auto-applies on startup like 008-011, but with split
non-transactional sections for index/FK validation.

```sql
-- 012_v2_data_pipeline.sql (transactional section)
ALTER TABLE mem_projects     ADD COLUMN git_remote          text NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN turn_index          int  NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN turn_subindex       int  NULL; -- multi-tool turns
ALTER TABLE mem_tool_calls   ADD COLUMN prev_user_prompt_id bigint NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN backfill_run_id     text NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN retention_class     text NULL DEFAULT 'live';
ALTER TABLE mem_user_prompts ADD COLUMN retention_class     text NULL DEFAULT 'live';
ALTER TABLE mem_user_prompts ADD COLUMN backfill_run_id     text NULL;
ALTER TABLE mem_user_prompts ADD COLUMN turn_index          int  NULL;
ALTER TABLE mem_user_prompts ADD COLUMN content_hash        text NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN content_hash        text NULL;
ALTER TABLE mem_tool_calls   ADD COLUMN truncated_at_bytes  int  NULL;

ALTER TABLE mem_tool_calls
  ADD CONSTRAINT mem_tool_calls_prev_user_prompt_fk
  FOREIGN KEY (prev_user_prompt_id)
  REFERENCES mem_user_prompts(id) ON DELETE SET NULL NOT VALID;
```

```sql
-- 012_v2_data_pipeline.concurrent.sql (run OUTSIDE transaction; runner must skip BEGIN)
CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_tool_calls_prev_user_prompt_id_idx
    ON mem_tool_calls(prev_user_prompt_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_tool_calls_session_turn_idx
    ON mem_tool_calls(session_id, turn_index, turn_subindex);
ALTER TABLE mem_tool_calls VALIDATE CONSTRAINT mem_tool_calls_prev_user_prompt_fk;
```

**Why each column:**
- `git_remote` — dedupes Dropbox-vs-local path forks of the same project.
  Falls back: `origin` → `upstream` → first remote → NULL. Passed through
  `redact_text()` to strip embedded creds.
- `turn_index`, `turn_subindex` — explicit ordering. Multi-tool turns share
  `turn_index`, differ in `turn_subindex` (position in content array).
- `prev_user_prompt_id` — the join column that fixes the linkage gap.
- `backfill_run_id` — rollback handle.
- `retention_class` — 'live' (default, subject to 30-day purge) | 'backfill_jsonl'
  (exempt) | reserved for #10's future classes.
- `content_hash` — row-level idempotency for partial-crash recovery.
- `truncated_at_bytes` — non-NULL marks `tool_input` over 16 kB; preserves
  the row instead of silently dropping content.

**Acceptance criteria:**
- [ ] Both SQL files exist; runner applies transactional section in `BEGIN`,
  concurrent section outside a transaction.
- [ ] Reversal `.down.sql` exists and drops columns + indexes + constraint.
- [ ] Auto-applies on `uvicorn` startup.
- [ ] `\d mem_tool_calls` shows all new columns + 2 new indexes + FK.
- [ ] `\d mem_projects` shows `git_remote`.
- [ ] Migration completes in < 10 s on a snapshot of the current 54k-row DB
  (measured: capture timing in `logs/m-ft-2/migration-012-timing.log`).
- [ ] `CREATE INDEX CONCURRENTLY` does NOT block concurrent hook POSTs
  (verified by a small load test: ApacheBench / `hey` against `/api/health`
  for 30 s while index builds).
- [ ] Existing 24 pytest tests pass unchanged.

**Testing:**
- `tests/migrations/test_012.py` — applies migration to a fresh DB, verifies
  columns + indexes + FK, then runs idempotency re-apply.
- `tests/migrations/test_012_reverse.py` — applies, reverses, re-applies.
- Load test script `scripts/test/migration_load_test.sh` (committed).

---

## Step 2 — Write `backfill_from_claude_jsonl.py`

**Issue:** #27

**Location:** `scripts/backfill/backfill_from_claude_jsonl.py` (new dir).

**Architecture:**
- Writes through canonical helpers in `app/main.py` (confirmed location:
  observation/prompt INSERT logic is there, not a non-existent `app/api/`).
  Any path that touches `mem_user_prompts` or `mem_tool_calls` goes through
  a new `app/backfill.py` module that wraps the existing writers and adds
  the recursive `redact_json()` step.
- New `app/redact.py` function `redact_json(obj) -> obj` walks dicts/lists,
  passes every string leaf through `redact_text()`. Specifically scrubs
  known nested paths: `headers.Authorization`, `headers.Cookie`,
  `*.api_key`, URL query strings with `token=`, `key=`, `password=`.
- Per-session atomic transaction: every `(session, prompts, tool_calls,
  results)` imports inside one `BEGIN/COMMIT`. Crashed session = fully absent.
- Memoized `cwd → git_remote` map within a single run.
- Observation queue **bypassed** for backfilled rows (set a flag the writer
  reads).

**Behaviour:**
- Reads every `~/.claude/projects/**/*.jsonl`.
- Each jsonl = one session. Filename UUID → `mem_sessions.session_id`.
- For each `user` message → `mem_user_prompts` row (deduped on
  `(session_id, turn_index, content_hash)`).
- For each assistant `tool_use` block → `mem_tool_calls` row with full
  `tool_input`, linked to immediately-prior user prompt of same session
  via `prev_user_prompt_id`.
- For each `tool_result` block → updates that tool_call's `response_preview`,
  `success`, `error`.
- Resolves `cwd` → `mem_projects` (insert if new). `git_remote` from
  `git -C <cwd> remote get-url origin` (fallback chain above), redacted.
- **Default = dry-run.** Reports counts of would-import sessions / prompts /
  tool_calls per project.
- Idempotent: dedupes on `(session_id, turn_index, role, content_hash)` for
  re-runs. Partial sessions detected and completed.
- Stamps every inserted row with `backfill_run_id` (run-start UTC) and
  `retention_class = 'backfill_jsonl'`.

**Edge cases (all 8 named with chosen behaviour):**

| # | Case | Behaviour |
|---|---|---|
| 1 | Malformed / truncated jsonl line | Skip line, log WARN, increment `parse.malformed_lines`. Continue file. |
| 2 | `tool_use` with no prior user prompt in file (continuation / `/resume`) | Insert row with `prev_user_prompt_id = NULL`, log INFO `orphan_tool_use`. |
| 3 | `tool_use` with no matching `tool_result` | Row inserted; `response_preview = NULL`, `success = NULL`. |
| 4 | Multi-tool turn (N `tool_use` blocks in one assistant message) | Same `turn_index`; `turn_subindex` = position in content array (0..N-1). |
| 5 | Very long `tool_input` (> 16 kB) | Store truncated to 16 kB; `truncated_at_bytes` set; full hash retained. |
| 6 | Unicode / invalid surrogates | Encode with `errors='replace'` before INSERT; log WARN. |
| 7 | `cwd` not a git repo | `git_remote = NULL`. `mem_projects.source_kind = 'local'`. |
| 8 | `cwd` missing on disk | Same as #7. Do not fail. |

**CLI:**
```
backfill_from_claude_jsonl.py [--commit] [--project-filter <glob>]
                              [--limit-sessions N]
                              [--jsonl-dir ~/.claude/projects]
                              [--batch-size 50]
```

**Pre-flight guards:**
- `pg_database_size('agent_memory') + estimate < 0.5 × free_disk` else abort.
- RSS soft cap 2 GB; warn at 1.5 GB.
- Max runtime 60 min; if exceeded, log and exit with code 2 (no rollback;
  partial sessions are atomic).

**Acceptance criteria:**
- [ ] Dry-run produces a per-project table of (sessions, prompts, tool_calls)
  to import; totals match raw jsonl block counts.
- [ ] Raw count formula (committed in test):
  `cat <jsonl> | jq -c 'select(.message.content[]?.type == "tool_use")' | wc -l`.
- [ ] `--commit` run produces ≥ 30k new `mem_user_prompts` rows.
- [ ] All inserted rows tagged with `backfill_run_id` and `retention_class`.
- [ ] Re-running with `--commit` is a no-op (idempotent at row level).
- [ ] **Crash recovery:** kill -9 the process during `--commit`, then re-run.
  Final counts match a single uninterrupted run (verified 3×).
- [ ] Secrets in jsonl (tokens in `Authorization`, `?token=…`, `sk-…`)
  redacted in stored content (synthetic seeded-secret fixture).
- [ ] `redact_json()` catches ≥ 99 % of seeded synthetic secrets in
  `tests/redact/seeded_secrets.jsonl`.
- [ ] Backfilled rows do NOT enter the observation-generation queue
  (verified by counter delta).

**Testing:**
- `tests/backfill/test_backfill_parser.py` — fixture jsonl with mixed
  user / assistant / tool_use / tool_result; assert row shapes.
- `tests/backfill/test_idempotent.py` — run twice, assert second run inserts 0.
- `tests/backfill/test_crash_recovery.py` — simulated mid-run abort + resume.
- `tests/backfill/test_redaction.py` — fixtures for nested Authorization,
  connection strings, env-var dumps, bearer in URL.
- `tests/backfill/test_edge_cases.py` — one test per edge case 1–8.

---

## Step 3 — Dry-run review + commit

**Issue:** #28

**Behaviour:** Run dry-run, paste per-project counts into issue #28 as a
comment. Spot-check 5 sessions by hand: pick the session_id, open the
matching jsonl, eyeball that parser's would-import rows match.

**Acceptance criteria:**
- [ ] Dry-run output committed to issue #28 (counts + 5 spot-check session IDs).
- [ ] No project shows > 10 % drop in tool_calls vs raw `jq` count of
  `tool_use` blocks in its jsonls (formula above).
- [ ] `--commit` run logged to `logs/m-ft-2/backfill-<utc>.log`.
- [ ] Post-commit row counts captured in the issue.
- [ ] Pre-flight checks (disk, RSS, runtime) passed.

---

## Step 4 — Audit live capture hooks

**Issue:** #29

**Files:** `hooks/session-start.js`, `hooks/pre-tool-use.js`,
`hooks/post-tool-use.js`, `hooks/session-end.js`.

**Investigate (root cause candidates):**
1. Missing insert in the user-prompt event path.
2. Wrong env-var gate (`AGENT_MEMORY_HINTS_ENABLED` confused with capture).
3. Race between session-start fire and the first user turn.
4. Auth header missing on prompt-write path (security sprint regression).
5. **Hook contract gap:** verify there IS a per-user-turn hook event. If
   Claude Code doesn't emit one, the fix may require a different mechanism
   (jsonl tail polling, or a Claude Code-side request).

**Acceptance criteria:**
- [ ] Root cause documented in issue #29 with code refs.
- [ ] Hook contract documented (which Claude Code events fire when).
- [ ] Fix lands as a hook patch (prior version saved to
  `hooks/legacy/<name>.pre-v2.js`).
- [ ] New session locally produces a `mem_user_prompts` row within 5 s of
  the first user turn — verified by automated polling test, not manual.
- [ ] Counter `linkage_ratio_24h` added to `/api/stats`.
- [ ] Counter `hook_error_rate` added to `/api/stats`.

**Testing:**
- `tests/hooks/test_user_prompt_capture.py` — POSTs the hook event payload
  directly to the API; polls `SELECT count(*) FROM mem_user_prompts
  WHERE created_at > now() - interval '5 seconds'` with 100 ms ticks; asserts
  > 0 within 5 s.
- `tests/api/test_stats_counters.py` — `/api/stats` returns the new metrics.

---

## Step 5 — Fix lookup/recall surface

**Issue:** #30

**The gap:** `/api/observations` (in `app/main.py`), session-start hints,
pre-tool-use hints, and `mcp_server.py` (repo root) `search` tool all return
narrative summaries. None surface `tool_input`.

**Changes:**

1. **`app/main.py`** — extend `/api/observations` response shape:
   ```json
   { "id": ..., "narrative": ..., "tool_calls": [
       {"name": "Bash", "input": {...}, "response_preview": "...",
        "success": true, "created_at": "..."}
   ]}
   ```
   **Join strategy (pinned, no `or`):**
   For each observation `obs` returned, attach `tool_calls[]` as the **union** of:
   - `(A)` `mem_tool_calls WHERE observation_id = obs.id`
   - `(B)` `mem_tool_calls WHERE session_id = obs.session_id
            AND created_at BETWEEN obs.created_at - interval '5 minutes'
                              AND obs.created_at + interval '5 minutes'
            AND observation_id IS NULL`
   Ordered by `turn_index, turn_subindex, created_at`. Deduped on
   `mem_tool_calls.id`. Capped at 5. Each `input` truncated to 2 kB.

2. **`hooks/session-start.js`, `hooks/pre-tool-use.js`** — hint generators
   include 1–3 representative `(tool_name, args)` examples from recalled
   observations. Gated by existing `AGENT_MEMORY_*_HINTS_ENABLED` flags.

3. **`mcp_server.py`** (repo root, not `app/mcp_server.py`) — `search` tool
   response shape mirrors #1, but `tool_calls[].input` field is **omitted by
   default**. Enable with `AGENT_MEMORY_MCP_EXPOSE_TOOL_INPUT=true`.
   Other fields (`name`, `response_preview`, `created_at`) always present.

4. **Feature flag:** entire response-shape change gated by
   `AGENT_MEMORY_RECALL_SHAPE` (`v1` default, `v2` after #28 lands).
   Tests flip flag; prod opt-in.

5. **Recall-time secret scan:** after building `tool_calls[]`, scan each
   `input` JSON one more time against the redaction patterns. If a known
   secret shape is found without a `[REDACTED:*]` marker, log WARN
   `redaction_miss_in_recall` and replace the field with `[REDACTED:LATE]`.
   This catches v1-era rows captured before redaction patterns were
   complete.

**Acceptance criteria:**
- [ ] `GET /api/observations?limit=5` with `AGENT_MEMORY_RECALL_SHAPE=v2`
  returns `tool_calls[]` populated.
- [ ] Mixed-origin session test (some calls with `observation_id`, some
  without) returns ALL tool_calls in the union, not just one set.
- [ ] `mcp__agent-memory__search` result objects include `tool_calls[]`
  metadata; `input` field present ONLY when env var is on.
- [ ] Auth tier verified: `/api/observations` requires the same auth as
  before. Test asserts 401/403 without token; 200 with.
- [ ] Session-start hint sample shows ≥ 1 `(tool, args)` pair.
- [ ] Contract snapshot test asserts pre-change response shape is a strict
  subset of post-change (all old keys present and unchanged semantics).
- [ ] `observation_recall_p99_latency_ms` exposed; baseline captured.
- [ ] `redaction_miss_in_recall_count` exposed.

**Testing:**
- `tests/api/test_observations_tool_calls.py` — seeded fixture with both
  origin types (`observation_id` set / NULL); assert union join.
- `tests/api/test_observations_auth.py` — auth tier regression.
- `tests/mcp/test_search_includes_tool_calls.py` — MCP search shape with
  env-var on AND off.
- `tests/api/test_recall_redaction_pass.py` — seeded missed-redaction row;
  assert warn log + `[REDACTED:LATE]` replacement.
- `tests/api/test_observations_shape_contract.py` — JSON snapshot of pre-
  v2 response shape; asserts v2 is a strict superset.

---

## Step 6 — Write `build_v2_dataset.py`

**Issue:** #31

**Location:** `scripts/fine_tune/build_v2_dataset.py` (canonical — matches
PR #24's `scripts/fine_tune/` convention).

**Behaviour:** single SQL query joining
`mem_tool_calls` → `mem_user_prompts` → `mem_sessions` → `mem_projects`,
per-session grouped into multi-turn Qwen 2.5 tool-call format.

**Output:** `data/processed/qwen25_tools/v2/` with same MANIFEST shape as v1.

**Per-row shape** (Qwen 2.5 chat-template-compatible):
```jsonc
{
  "messages": [
    {"role": "user", "content": "<real user prompt>"},
    {"role": "assistant", "content": null,
     "tool_calls": [{"name": "Bash", "arguments": {...}}]},
    {"role": "tool", "name": "Bash", "content": "<response_preview>"},
    {"role": "assistant", "content": "..."}
  ],
  "tools": [<tool schemas with descriptions, see Step 7>],
  "source": "claude_jsonl",
  "session_id": "<uuid>",
  "bash_command": "git",
  "synthetic": false
}
```

**Filters:**
- Drop rows where `tool_input` is empty `{}` **AND** the tool schema has
  non-empty `required[]` (only the v1 loop-bug shape, not legitimately
  empty-args tools).
- Drop sessions with < 2 turns.
- **Bash sub-classification:** for any row where `tool_name = 'Bash'`, parse
  `tool_input.command` and record the first non-flag token as
  `bash_command` (e.g., `git`, `pytest`, `psql`, `gh`, `make`, `curl`,
  `find`, `grep`). Cap each `bash_command` distinct value at 20 % of the
  Bash slice. Non-Bash tools each capped at 20 % of total.
- **Discard strategy:** stratified down-sample by `(prompt-shape-hash,
  bash_command, success)`, preferring rows with longer arguments and
  distinct prompts. Preserve ≥ 200 unique prompts per (Bash sub-command or
  non-Bash tool).

**Acceptance criteria:**
- [ ] ≥ 25k rows in `v2/train.jsonl`.
- [ ] < 10 % synthetic rows (sentinel-tagged for audit).
- [ ] No row has empty `arguments` unless schema permits empty.
- [ ] Tool + `bash_command` distribution histograms saved to MANIFEST.
- [ ] Max share ≤ 20 % per (tool | bash_command); attached as PR evidence.
- [ ] Per-row chat-template render passes `tokenizer.apply_chat_template`
  on 100 sampled rows.

**Testing:**
- `tests/fine_tune/test_v2_dataset_shape.py` — sample 50 rows, assert schema.
- `tests/fine_tune/test_v2_no_empty_args.py` — empty-args filter respects
  schema `required[]`.
- `tests/fine_tune/test_v2_template_renders.py` — `apply_chat_template` on
  100 sampled rows, assert no exception.
- `tests/fine_tune/test_v2_bash_subclassification.py` — assert `bash_command`
  populated for every Bash row.

---

## Step 7 — Retrain v2

**Issue:** #32

**Reuses:** existing pipeline from PR #24 (Phase 1-6 of `PIPELINE_RUNBOOK.md`).

**Deltas vs v1:**
- **1.0 epoch** (v1 was 0.5).
- Tool **descriptions** added to schemas (v1 had names only).
- v2 dataset path.
- New run dir under `models/lora/qwen25-3b-toolcalls-lora/runs/<UTC>/`.

**Acceptance criteria:**
- [ ] All phase gates in `PIPELINE_RUNBOOK.md` pass.
- [ ] Validator pass rate ≥ 85 % (v1 was 80 %), per
  `scripts/fine_tune/validate_tool_calls.py --pass-threshold 0.85`.
- [ ] New eval: 50 vague natural prompts from
  `tests/fine_tune/fixtures/vague_prompts.txt`. **0** empty-args emissions.
- [ ] GGUF Q4_K_M at `models/gguf/qwen2.5-3b-toolcalls-v2-q4km.gguf` with
  `.sha256` sidecar.
- [ ] Per-run report at `docs/training_runs/v2-<UTC>.md`.
- [ ] `empty_args_emissions_total{model=v1,v2}` counter exposed in
  `/api/stats`; pre-deploy baseline (v1) and post-deploy week-1 (v2) both
  captured.

**Testing:**
- Existing 24 pytest tests (`tests/fine_tune/`) must pass.
- New: `tests/fine_tune/test_no_empty_args_eval.py` against the 50-prompt set.
- New: `tests/api/test_stats_empty_args_counter.py`.

---

## Step 8 — Anti-loop inference guard

**Issue:** #33

**Independence note:** #33 has **no schema or data dependency**. It can
land at any time, including before #26. Re-ordered to early in the
sequence for safety-net reasons.

**Location:** `scripts/fine_tune/validate_tool_calls.py` — add `--anti-loop`
flag.

**Behaviour:** detect 3 consecutive identical tool calls (same name +
arguments, normalized) within a conversation; on the 3rd, force a text
response by discarding the `tool_call` block. Log WARN with model version
tag. Increment `empty_args_emissions_total` counter.

**Acceptance criteria:**
- [ ] Flag exists, default off.
- [ ] Fixture conversation with intentional loop → with `--anti-loop`, the
  3rd call is suppressed; without, all 3 pass through.
- [ ] WARN log on suppression includes `model_version`.
- [ ] Documented in `FAILURE_MODES.md` as the canonical mitigation.

**Testing:**
- `tests/fine_tune/test_anti_loop.py` — synthetic 3-repeat conversation,
  assert suppression on/off behaviour.

---

## Order of operations

```
#33 anti-loop guard (independent, safety-net) ──► land first

#26 schema 012 (transactional + concurrent split)
       │
       ▼
   ┌───┴────────────┐
   │                │
   ▼                ▼
#27 backfill    #29 hook audit
   script           (independent files)
   │                │
   ▼                │
#28 dry-run         │
   + commit         │
   │                │
   └───────┬────────┘
           ▼
#30 recall surface fix (env-flagged; default v1 until tests flip)
           │
           ▼
#31 v2 dataset build (needs #28 data + #30 shape for consistency)
           │
           ▼
#32 retrain + eval
           │
           ▼
   close #25 (parent)
```

**Hard dependencies:**
- #27 needs #26's `backfill_run_id` column.
- #28 needs #27.
- #30 needs #26 (no schema collision); ships dark (env flag).
- #31 needs #28 (data) and #30 (shape for tests).
- #32 needs #31.

**Soft dependencies (can parallelize):**
- #29 reads no schema additions; safe to run alongside #27.
- #33 reads nothing from this plan; should land first as the safety net.

## Sanity checks before starting

```bash
git status                                              # clean, on feat branch
gh pr view 24 --repo metazen11/agent-memory             # MERGED
curl -s http://localhost:3377/api/health | jq           # service up
psql --version                                          # PG 16.13 (verified)
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_tool_calls"     # 54,987
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_user_prompts"   # 1,410
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_sessions"       # 893
psql -U mz -d agent_memory -c "SELECT count(*) FROM mem_projects"       # 686
find ~/.claude/projects -name '*.jsonl' -type f | wc -l                 # ~2,367
.venv-finetune/bin/python -m pytest tests/fine_tune/ -q                 # 24/24
```

---

## Quality control — per-issue gate

Every sub-issue (#26–#33) must pass a **QC gate** before its PR merges.

### QC gate checklist (applies to every sub-issue)

1. **Acceptance criteria** — all `[ ]` boxes in the step's section ticked,
   with PR-body links to where each is satisfied (file:line or test output).
2. **Tests** — all tests in the step's "Testing" subsection exist and pass.
   CI green. No skipped / `xfail` tests without a follow-up issue link.
3. **Regression guard** — `pytest tests/ -q` passes; existing 24
   `tests/fine_tune/` tests untouched.
4. **Observability** — every new code path has BOTH a test AND a log line at
   INFO+. User-visible changes additionally emit a metric exposed via
   `/api/stats`. (Strengthened from "one of three".)
5. **Rollback note** — PR body has the exact revert procedure (env var,
   migration down, etc.) from this plan's Rollback section.
6. **Docs** — `PIPELINE_RUNBOOK.md` and/or `FAILURE_MODES.md` updated if the
   change alters operating procedure or introduces a new failure surface.
7. **Quality-gate agent review** — for #26, #27, #30, #31, #32: the
   `quality-gate` agent must be run on the PR diff and its JSON findings
   pasted into the PR before merge.
8. **CLAUDE.md compliance** — review precedes test, DRY check applied, no
   `Co-Authored-By` lines.
9. **Path canonicalization** — every file path in PR description verified
   to exist in repo (`scripts/check_plan_paths.py` if it lands; manual
   otherwise).

### Per-issue QC additions (on top of the shared gate)

| Issue | Extra QC requirement |
|---|---|
| #26 schema | Migration applies + reverses cleanly on a snapshot. Load test confirms CONCURRENTLY does not block hook POSTs. |
| #27 backfill script | Dry-run output diffed against jsonl `jq` block counts; ≤ 10 % delta. Crash-recovery test passes 3× consecutively. |
| #28 dry-run/commit | Spot-check 5 randomly sampled sessions by hand vs raw jsonl. Pre-flight guards (disk, RSS) recorded. |
| #29 hook audit | Automated 5-s prompt-capture test passes. `linkage_ratio_24h` returns sane value. |
| #30 recall surface | Manual `curl` + MCP call show populated `tool_calls[]`; auth tier test green; MCP env-var off-by-default verified. |
| #31 v2 dataset | Histogram + bash_command distribution attached to PR. Sentinel-tagged synthetic count attached. |
| #32 retrain | 50-prompt empty-args eval result (`0` loops) attached to PR. `empty_args_emissions_total` counter verified in `/api/stats`. |
| #33 anti-loop | Loop fixture pre/post `--anti-loop` attached. Lands first as safety net. |

### Closing the parent (#25)

The parent issue closes only after **all 8 sub-issues are merged AND** the
overall acceptance criteria (top of this doc) are satisfied. Final close
comment must include:
- Final row counts (`mem_user_prompts`, `mem_tool_calls` linkage %).
- V2 GGUF path and SHA.
- Eval numbers vs v1.
- 7-day post-deploy `empty_args_emissions_total{model=v2}` reading.
- Updated handoff section pointer in `HANDOFF.md`.

## Quality-gate review (holistic, plan-level)

Full review JSON at `docs/fine_tune/reviews/25-quality-gate.json` (verdict:
`approve_with_changes`, 8 blockers + 6 non-blockers). All blockers addressed
in this revision; non-blockers F7, F8, F9, F11, F12, F14, F15, F16, F18 also
addressed. Open items for future plans tracked in the review's
`improvements` section.
