## 2026-10-05 — Supervised service and spool recovery

Failure: startup raced PostgreSQL readiness, API exited and was never restarted.
No host watcher was running; native Desktop hooks only spooled, and local drain
could not discover other repos/worktrees. Initial inventory: 1,502 JSON files.

Added login LaunchAgents for API restart and a global bounded replay pass every
60 seconds. Working root stays `~/_CODING`; no symlink or Dropbox traversal.
Atomic spool writes and per-file locks preserve queued records through exits.
Migration 018 replay receipts commit atomically with API writes; legacy spool
copies share stable identity. Malformed/validation failures are quarantined,
transient failures retain files. Status: `~/.codex/agent-memory-recovery.json`.

CODE_REVIEW before TEST covered transaction boundaries, lost acknowledgment,
lock ownership, permanent versus transient errors, source project/session
preservation, supervised process ownership and discovery boundaries.

Verification: **427 passed, 2 skipped**, with the existing embedding-library
warning. Ruff, Python compile and Node syntax checks passed. Killed only the
verified launchd-managed API process with SIGKILL: it recovered with a new PID
and healthy DB in **23.5 seconds**. The recovery job retained unacknowledged
files during the outage and resumed: first successful full pass drained 40
files; cumulative 46 acknowledged, zero quarantines, 1,629 pending across 25
spool directories at verification. Backlog continues draining every minute;
these counts are a timestamped sample, not a completion claim.

Installed jobs: `com.metazen.agent-memory-api` (KeepAlive) and
`com.metazen.agent-memory-recovery` (60-second bounded passes, retry on exit).
Rollback: installer `uninstall` stops/removes both jobs while preserving data.
Migration 018 has a down script; dropping receipts forfeits replay dedupe, so
retain it unless deliberately reverting the whole ingestion change.

## 2026-10-03 — PR #69 automated review follow-up

Addressed three GitHub review comments: source occurrence identity replaces
anonymous per-hash counts; offline file glob matching follows Python fnmatch;
strict API and JavaScript scope comparisons canonicalize historical aliases to
local paths. Dropbox remains archival; working files and services use
`/Users/mz/_CODING`. No filesystem operations use the archive path.

Reviewed identity/project guard, glob parity and scope isolation before TEST.
Final verification: **422 passed, 2 skipped** (one existing embedding warning).
Ruff, Python compilation, Node syntax and whitespace checks passed. A separate
33,099-case comparison against Python fnmatch found zero mismatches.
New preview reports 465 source occurrences without exact identities in the
existing data; old live captures may have server timestamps. This is an audit
candidate count, not proof that all are missing. No prompts or tool links were
rewritten/imported during this follow-up. Earlier count-based zero-gap claims
are superseded by occurrence-based reconciliation. Audit report:
`logs/codex-backfill/review-occurrences-preview.json`.

Hint visibility: only current lesson is warning #77, scoped to `.mcp.json`;
there are no current-project critical prompt lessons. Native prompt/pre-tool
hooks lack trust state; automatic delivery is not yet verified. Existing
SessionStart context is visible to the model. Hook definitions still need the
user's Codex trust review; no trusted hashes have been fabricated.

## 2026-10-03 — Pre-PR code review

User requested review before push/PR. Review found and fixed: failed native
prompt writes now spool and recover without session state; live Codex queue
writes now resolve prior prompt IDs within the same native session/project;
strict project scope uses literal `starts_with` rather than wildcard SQL LIKE.
Patch lesson checks cover apply_patch/Edit/Write aliases. Trigger tracking
sockets are unref'd so the 2-second pre-tool hook budget is preserved. Backfill
parser rejects scalar/malformed records; default audit files have unique run
IDs and are not overwritten by later runs. New tests cover offline recovery,
patch aliases, wildcard paths, and live prompt-to-tool isolation.

Full suite before these review fixes: 397 passed, 2 skipped. Final reviewed gate: **401 passed, 2 skipped**, with one pre-existing
embedding-library deprecation warning. Ruff checks now pass for all
changed Python files; new script/tests were formatted for readability.

## 2026-10-03 — Codex transcript prompt backfill

User authorized restoring missed history after the hook repair. Built
`scripts/backfill/backfill_codex_prompts.py`: reads active + archived rollout
JSONL, excludes injected context/delegated-agent messages, preserves native
session IDs and source UTC timestamps, redacts secrets, normalizes Dropbox
paths, and resolves canonical git projects. Original cwd can change per turn.

Uses `scripts/psql_wrapper.sh` exclusively; no credentials printed. Default is
preview, `--commit` performs paced (650 ms), atomic per-session imports.
`backfill_run_id` plus reports under `logs/codex-backfill/` provide resumability
and audit evidence. Repeated human turns remain distinct. Existing prompt
hash counts are consumed during planning; exact session/hash/time guards prevent
repeated writes. Existing records are not renumbered or moved to other projects.
Optional `--link-tools` fills missing links only within the same session and
project and to the nearest preceding prompt. It never replaces an existing link.

CODE_REVIEW completed before TEST: SQL literal escaping, transaction isolation,
resumption, source filtering, path normalization and secret redaction reviewed.
Parser + hook regression suite: **17 passed**. Live pilot imported one prompt;
second planning pass skipped it. Completed: **1,699 prompts restored across 91 sessions**, spanning Dec 7,
2025 through Oct 3, 2026. Pilot: 1 prompt; full run: 1,698 prompts in 90
sessions. **7,222 existing tool calls linked** to the nearest preceding prompt
in the same native session and project. Database verification found **0 invalid
links**. Repeat preview: **0 missing prompts**; repeat linkage: **0 changes**.
528 context/empty messages and 650 delegated messages excluded. Existing prompts
are preserved; no historical synthetic session IDs were heuristically merged.

Audit report: `logs/codex-backfill/completed.json` (includes tool/prompt IDs),
preview/recheck: `preview.json`, `verification.json`; UTC progress: `import.log`.
Rollback for links: clear prev_user_prompt_id only for the report's tool IDs
still pointing at the recorded prompt IDs. Then delete imported prompt rows
for the report run_id and pilot_run_id. Existing tool rows remain intact.
No schema migration, rate limiter change, remote push, or PR.

## 2026-10-03 — Codex capture and hints repair

Investigation confirmed working MCP recall, SessionStart context, and 825 Codex
Bash records in the previous day. Gaps: no live Desktop prompt recorder
(history.jsonl stale since Sep 20), synthetic SessionStart IDs split session
records, MCP/local tools excluded by PostToolUse matcher, and PreToolUse never
queried file_scope lessons or emitted model-visible additionalContext.

Repair adds native UserPromptSubmit capture plus capped critical hints, native
session IDs for start/end, file_scope/patch-path hint delivery, and all-local-tool
PostToolUse capture. Registration updates matcher in place and appends new
handlers, preserving existing hook indices. Changed/new host definitions need
user trust review through Codex hook settings; no trust hashes are forged.
Ordinary ChatGPT Chat has no local hook runtime; Work requires script/service
availability. API health still reports Anthropic billing_error, with database
and embeddings healthy and a local LLM fallback configured.

CODE_REVIEW checked scoping, output contracts, session identity, DRY helper,
timeout budgets, and registration order before TEST. Focused verification:
`tests/test_codex_delivery.py`, `tests/test_codex_hook_contract.py`, and
`tests/test_api_lessons.py`: **27 passed, 1 skipped** (database correctly rejects
a legacy broad-match fixture). `.venv-finetune` lacks pytest; `.venv/bin/python`
is working for this suite. Live API restarted gracefully via ensure-services,
strict-scope endpoints verified with current-project lessons, latest user prompt
saved under native session ID, and warning #77 delivered through the Node hook
for an `.mcp.json` patch. No patch was executed by that diagnostic.

Registered only Codex hooks: existing entries stayed in place; prompt handler
appended; PostToolUse matcher updated. Activation of new/changed definitions
requires user trust review in Codex hook settings, then chat reopen. PreToolUse
also had no saved trust entry. No config trust records were modified. Legacy
history drain now opt-in (`AGENT_MEMORY_CODEX_HISTORY_FALLBACK=1`).

An existing session checkpoint hook committed the first seven changed files as
`76a55f6` during this work; subsequent edits remain in the working tree. No
remote push or PR was created. Memory observation #100860 preserves diagnosis.

# Handoff

## 2026-10-02 — trunk reconciled, three live pipeline defects fixed

Opened as "agent memory isn't updated or running with hooks." It was running:
capture had never broken (tool calls and prompts landing seconds apart, 74k /
2.4k rows). The actual fault was that **this clone was 18 commits behind
`main`**, so the hooks — all five correctly symlinked into this repo — were
executing last month's code.

**PR #67 (`dev` -> `main`) is OPEN, MERGEABLE/CLEAN, 4/4 checks green.**
Left unmerged deliberately: CONTRIBUTING makes that PR the human review gate.

### What landed

1. **Trunk reconciled** (18 commits). Brought in the branching contract,
   `trunk-drift` + `contract-integrity` CI, `.githooks/pre-push`, ADR 0001,
   and the MCP portability fix (#56).

2. **Two active fixture lessons were being injected into live sessions.**
   173 of 199 lessons were pytest fixtures; 171 were already `active=false`
   and harmless, but `#93 "bucket probe"` (rule: *"probe rule that is long
   enough to pass validation checks ok"*) was appearing verbatim in the
   `Active Lessons` block, and `#91` was scoped to project 47215, which does
   not exist. Both deactivated -> 25 active lessons, all real.

3. **Root cause of #66 fixed** (commit on `dev`). Cleanup lived in
   `test_api_lessons.py::test_deactivate_lesson`; a test only runs if
   collection reaches it, so `-k`, `-x`, an earlier failure or Ctrl-C skipped
   it. Now a session-scoped `autouse` teardown.

4. **42 queue rows wedged in `processing`**, oldest since 2026-03-28 —
   4,498 hours. Requeued; queue has fully drained to zero `processing`.

### `.mcp.json` — read before touching

`main` had moved to `args: ["-c", "exec \"$CLAUDE_PLUGIN_ROOT/..."]`. Correct
for PLUGIN scope and the convention #56 documented — but this repo's
`.mcp.json` is ALSO read as PROJECT scope, where the var is unset. Measured
rather than argued:

```
$ env -u CLAUDE_PLUGIN_ROOT sh -c 'exec "$CLAUDE_PLUGIN_ROOT/scripts/run_mcp.sh"'
sh: /scripts/run_mcp.sh: No such file or directory      # the ENOENT from #48
```

Both historical claims were true about different scopes. Resolution keeps
both: `command: "sh"` (bare interpreter, satisfies every portability guard)
plus a fallback loop trying `${CLAUDE_PLUGIN_ROOT:-}` FIRST, then known
roots. `test_mcp_manifest.py` 6/6. Lesson #48 was rewritten to say this —
as written it would have told the next session to reject main's correct fix.

### Anthropic provider — still unpaid for THIS key

Reported as paid; it is not, for this key. Verified live, not from the cached
snapshot (fresh `req_011CfdTkVdRd5kVadeen2aAt`):

- `models.list` -> **SUCCESS** (returns sonnet-5-5, opus-5-5, fable-5-1), so
  the key authenticates and is not revoked
- `messages.create` -> **400, credit balance too low**

Auth fine + payment refused means the credit went to a different
account/workspace than this key's org. Key itself is clean: `sk-ant-api03-`,
108 chars, no whitespace or stray quotes, fingerprint `4570ac24446`
(sha256 prefix). Check console.anthropic.com -> Plans & Billing, then confirm
that key sits in the funded workspace. A Claude.ai Pro/Max subscription does
NOT fund API usage.

Not blocking: local daemon on `:3399` is healthy, so distillation runs on the
local model — degraded quality, not an outage.

**Filed #68**: `BILLING` is in `NON_TRANSIENT`, so the breaker latches for the
process lifetime, and `reset()` — which documents itself as "for an explicit
operator retry" — has no route. No way to clear it without restarting uvicorn,
and `/api/health` reports stored state, so an operator cannot distinguish
"still broken" from "fixed but not yet told."

### Verified end state

| Check | Result |
|---|---|
| Full suite on merged `dev` | **385 passed, 2 skipped** |
| `test_mcp_manifest.py` | 6/6 |
| Active fixture lessons, any scope | **0** (was 2) |
| Active real lessons | 25, untouched |
| Queue stuck >24h | **0** (was 42) |
| `dev` behind `main` | **0** — contract rule 2 satisfied |
| Everything committed + pushed | yes, `dev` and `work/session-20260923` both ahead 0 |

### Next

- Merge PR #67 (your call — it is the review gate)
- Sort Anthropic billing on the right workspace, then restart uvicorn to clear
  the latched breaker (or implement #68)
- `origin/dev` had one commit I was missing (`3a73785`, #58); now merged in
- `core.hooksPath` set in THIS clone; any other clone needs
  `~/_CODING/hooks/repo-contract/bootstrap.sh` once per machine

### Caveat on my own work

The first version of the #66 teardown swept only the default scope, **passed
its own verification**, and still leaked `#261 'Test lesson'` under project
67850 on the next full run — `GET /api/lessons` without `project` returns only
`project_id IS NULL` rows (deliberate). Caught by re-querying Postgres instead
of trusting a green suite. Now sweeps global + `test_project`, deduped by id,
verified under both a full run and a `-k` partial run (3 passed, 10
deselected — the exact shape that produced the original 173 rows).

## 2026-09-23 — hints delivery, automatic distillation, syntactic search

All three landed and are verified. Full suite: **342 passed, 1 skipped**,
with rate limiting ENABLED (issues #63 and #64 also fixed, see below).

NOTE: earlier runs in this session were described as "rate limiting
disabled" using `AGENT_MEMORY_RATE_LIMIT_ENABLED=false`. That variable
does nothing — `Settings` has no `env_prefix`, so the real name is
`RATE_LIMIT_ENABLED`. Those runs actually had limiting ON.

### 1. Hints were structurally undeliverable (commit 35a4e76)

`hooks/pre-tool-use.js` queried `/api/lessons/match` without `trigger_on`
or `modified_files`. The endpoint defaults `trigger_on='input'` and
filters `WHERE l.trigger_on = $1`, so **every** `file_scope` lesson — all
five created on 2026-09-23 — was unreachable from the hook. Editing
`.mcp.json` reported "no active lessons match" while the same call
against the API returned the CRITICAL lesson.

It survived because verification hit the API and never the hook. Same
write-path/read-path seam as the Anvil `default_stack()` bug. Tests now
assert **through the hook process**; 3 of 4 fail against the old hook.

### 2. Lessons are now created automatically (645ba9c, 1a16ff4)

`app/lesson_distill.py` mines recurring failures from `mem_tool_calls`.
Design is grounded in what the data actually shows — see the README table
for the full rationale. The two non-obvious calls:

- **Threshold on repetition, not distinct sessions.** 5,511 of 5,640
  recent failures came from ONE long-running session; a "seen in >= 3
  sessions" rule would have mined almost nothing.
- **Triggers are derived in code, never from the LLM.** Asked for a
  trigger, a model returns the broad-match shape migration 016 refuses.

Schedule installed: launchd, Sunday 04:07 (`scripts/install_distill_schedule.sh --check`).
5 lessons were created on the first real run and verified firing through
the hook, correctly scoped to their project.

### 3. Syntactic search (de3e1da)

FTS used `to_tsquery`, which parses raw tsquery syntax — `foo()`,
`file.py:42`, `a & b` all returned **500**. Now `websearch_to_tsquery`.
Added `mode="literal"` plus an exact-phrase pass in the MCP search tool
(fused at k=30, above the k=60 semantic pass).

### Known issues / next steps

- **`ANTHROPIC_API_KEY` has no credit.** Still true and still yours to
  fix — topping it up remains the single highest-leverage change for
  lesson quality. But it is **no longer silent (#64, a7be30a):**
  `/api/health` now reports `billing_error` with the breaker open, a run
  stops retrying after the first billing failure instead of re-paying the
  13s throttle per candidate, and lessons record `synthesized_by` so
  locally-written ones can be found and re-synthesized later.
- ~~Rate limiting breaks the test suite~~ **FIXED (#63, c26cf46).** The
  limiter now keys on agent identity with a per-scope segment. Two traps
  found on the way: the suite was itself sending `X-Agent-Name: claude`
  (identical to live hooks), so keying on agent name alone would have
  fixed nothing — tests now identify as `pytest`; and the key was
  `{client}:{method}` while limits vary by PATH, so `/api/admin` (cap 10)
  and ordinary reads (cap 500) shared a bucket.
- **Session summaries + budgeted SessionStart injection** remain the real
  claude-mem parity gap. Unstarted. `session-start.js` retreated from
  injection after a 15KB block blew the ~2KB cap; the fix is a budget
  fitter, not omission.

---

## Earlier — Resume after reboot — 2026-09-20: remaining Codex hook errors

**User paused troubleshooting to reboot. No hook repair has been applied in this session.**

### Confirmed diagnosis

- Workspace: `/Users/mz/_CODING/agentMemory` (shell renders `_coding` on this Mac).
- Git was clean at the start; HEAD was `fa8e73a` (`fix(hooks): keep codex session end output contract-safe`), following `fdeac01` (`fix(hooks): repair agent-memory host wiring`). These earlier fixes are already present.
- `~/.codex/hooks.json` still registers **missing files**:
  - `~/.codex/hooks/git-session.js`: SessionStart, PreToolUse, SessionEnd.
  - `~/.codex/hooks/env-guard.js`: PreToolUse.
- Both source files still exist:
  - `/Users/mz/_CODING/hooks/git-session/git-session.js`
  - `/Users/mz/_CODING/hooks/env-guard/env-guard.js`
- The four agent-memory hook symlinks in `~/.codex/hooks/` already point at this checkout and their targets exist. `no-attribution.js` also exists.
- Missing registered scripts are a concrete failure source; the precise UI error has not yet been captured. Today's Codex desktop log search did not return matching hook errors. Do not claim every reported error is explained or fixed yet.

### Next actions

1. Recheck `~/.codex/hooks.json`, target existence, and Git status after reboot.
2. Finish reviewing the source hooks for Codex compatibility, then restore the two missing symlinks. Writing under `~/.codex/hooks/` requires sandbox escalation. Use `ln -s` without force so an unexpected existing file is preserved:
   ```bash
   ln -s /Users/mz/_CODING/hooks/git-session/git-session.js /Users/mz/.codex/hooks/git-session.js
   ln -s /Users/mz/_CODING/hooks/env-guard/env-guard.js /Users/mz/.codex/hooks/env-guard.js
   ```
3. Verify each registered hook script exists. Test hooks using isolated temporary fixtures: **git-session can initialize repos, create branches, commit, and push**, so do not smoke-test lifecycle handlers against this live workspace. Its PreToolUse output uses `permissionDecision: allow`; env-guard uses allow/deny. Confirm these match the installed host contract.
4. Run CODE_REVIEW before TEST, then relevant hook integration checks (`tests/test_codex_hook_contract.py`) if code is changed. Existing tests invoke the local memory service; inspect isolation before running them. No tests or repairs were run before the reboot pause.
5. Confirm a fresh Codex session no longer reports the errors; capture exact remaining errors if any. Update this handoff, task state, and README if implementation changes are made.

### Useful context

- Existing `scripts/repair-agent-memory-hooks.js` repairs agent-memory wiring only; it does not restore these unrelated git-session/env-guard files.
- Source hook repository README was read. No source files there were edited. Read its applicable instructions before any edits.
- Official hook reference: https://learn.chatgpt.com/docs/hooks (opened via https://developers.openai.com/codex/hooks). OpenAI Docs skill was consulted.
- GitHub open issues were read; the ten latest concern fine-tuning and the web UI, with no matching hook task in that limited listing. No issue was created.
- `todo.json` was read and retains older project tasks. This interruption is tracked in this handoff; no implementation task was completed.
- Memory lookup returned historical hook-wiring context, not a resolution for these missing files.
- User's initial “where did everything go?” remains ambiguous. The project directories (`app`, `models`, `data`, `fine-tune`, `docs`, etc.) are present; no deletion was established.

---

## Earlier handoff (historical; dates and pending actions below are stale)

> **2026-05-18/19 infra sprint (separate track):** lesson-scope leak fixed,
> session-start preamble shrunk 97%, new `recall()` + `abilities_memory()`
> MCP tools, anvil reached lessons-inject parity with claude, integration
> guide at `docs/INTEGRATION.md`, migration 015 quarantines super-projects
> on fresh DBs, `tool_calls` router mount bug fixed (`/api/tool-calls` was
> 404 since forever), codex per-turn-lessons gap specced at
> `docs/sessions/codex-parity-todo.md`. Full write-up:
> `docs/sessions/2026-05-19-memory-infra.md`. **8 commits on `dev`;
> integration PR #51 (`dev → main`) is open and MERGEABLE
> (fast-forward).** Stream 2 commits in the same PR are the v4/v4.5
> fine-tune sprint — confirm intentional before merging.

## State (2026-09-25)

**All three repos: `dev` and `main` in sync, clean, CI green.** No work in flight.

| Repo | main | Notes |
|---|---|---|
| agent-memory | `a345fe0f` | 299 tests passing |
| agent-hooks | `af6da4c7` | 51 hook tests passing |
| anvil | `8b4562a8` | 3612 tests passing (2 known failures, see below) |

## Shipped this cycle

**Branching contract, enforced in 4 layers** (was: markdown in `~/.claude/`, which
does not travel between machines — that is why work reached `main` from another
machine without passing through `dev`):

1. **Branch protection** on `main`, `enforce_admins: true` — server-side, no setup.
   Verified by an actually-rejected push, not by reading config.
2. **`trunk-drift`** workflow — fails when `main` has content `dev` lacks.
3. **`.githooks/pre-push`** — refuses direct pushes to `main`.
4. **`CONTRIBUTING.md` + README section** — so a blocked push explains itself.

**Auto-install:** `~/_CODING/hooks/repo-contract/bootstrap.sh` — one command per
MACHINE. Installs a git template (future clones self-activate) and retrofits
existing clones. Git forbids a clone activating its own hooks, so this step
cannot be removed, only moved.

**Issues fixed:** #56 (MCP release blocker — CLOSED), #341 (eslint — CLOSED),
#57 (path leakage), #58 (call-count metric).

## Next

1. **Run `bootstrap.sh` on the OTHER MACHINE.** Its clones have no local pre-push
   hook until then. Server-side layers still cover it.
2. **Restart Claude Code** if agent-memory MCP misbehaves — `.mcp.json` moved to
   the portable `sh -c "$CLAUDE_PLUGIN_ROOT/..."` form and needs a fresh spawn.
3. **Three stale PRs need a human decision** — all are feature-branch → main,
   which the contract forbids (PRs must be `dev → main`):
   - agent-hooks **#12** (CONFLICTING, Sep 21)
   - agent-memory **#54** (CLEAN but from Jun 5), **#44** (CONFLICTING, May 15)
   Close them, or rebase the work onto `dev` and let it promote normally.

## Open issues, with findings recorded

- agent-memory **#57** — source of path leakage is FIXED (581/5000 rows → 0,
  measured). The published `WFCA/qwen3-4b-toolcalls-v5-pilot` weights still carry
  memorized paths; retrain-or-retract is still open.
- agent-memory **#58** — over-emission is now MEASURABLE (`--min-exactly-one-rate`,
  default report-only). Model root cause (training data vs stop-token) still open.
- anvil **#342** — flaky test, NOT reproducible across isolation/pairwise/full-suite
  fixed-order runs. No retry or xfail added; that would hide the mechanism.
  `pytest-randomly` is not installed. Suspect git racy-index; untested.
- anvil **#343** — BLOCKED: `make pyright` does not run at all (version pin), so
  the 141-error baseline cannot be measured, let alone ratcheted.
- agent-hooks **#11** — transcript full-read per turn (~190ms/276MB on 50MB).
  Ships at the 5s timeout, but `context-primer.js` ALSO still counts tool results
  as user turns (the bug fixed in `iron-rules.js`). Fix both in one pass.

## Known-failing, pre-existing

anvil `test_projects.py::test_web_project_scheduled_task_endpoint_creates_launchd_job`
and `::test_web_project_plugins_endpoint_lists_and_registers` — identical on
pristine `dev`, unrelated to recent work.

## Context

- **Use `.venv-finetune`** for Python here; `.venv` is a corrupted Dropbox mirror.
- **`gh auth status` is NOT git's push identity.** Repos are pinned with
  `git config credential.https://github.com.username metazen11`. If a push 403s or
  a fetch silently no-ops, pass the token explicitly — a failed fetch leaves a
  STALE `origin/main` and will make you report a false state.
- v5 model: **`WFCA/qwen3-4b-toolcalls-v5-pilot`** (org, not WFCAMZ), quant
  **Q4_K_M**. Test via LM Studio.
- Do NOT kill the git-session checkpoint hook — its handoff marker was the only
  pointer to an anvil commit unreachable from both trunks.

Codex hints use `strict_scope=true`: only the current cwd or ancestor project
scopes qualify; unscoped, child, and sibling lessons are excluded. Prompt and
pre-tool hints send the same capped text as a visible `systemMessage` and
model-visible `additionalContext`, prefixed with the current project path.

Pre-PR checks: Ruff passed for all changed Python files, Python compileall
passed, Node syntax checks passed for 12 integration/installer files, whitespace
and credential-pattern scans passed. No remaining blocking review findings.
Publication uses one clean commit on `codex/codex-memory-repair`, then the
required integration promotion through `dev -> main`. Hook activation still
requires the user's trust review of new/changed host definitions.

Published `e830fcd` to `codex/codex-memory-repair` and `dev`. Promotion PR:
https://github.com/metazen11/agent-memory/pull/69 (`dev -> main`), open and
mergeable. Local gate: 401 passed, 2 skipped; remote push checks passed.
The PR is attached to the Codex chat. Merge remains the human review gate.

## 2026-10-05 — CI/CD and Anvil fallback

Branch `codex/cicd-anvil-fallback` adds a hosted quality gate, feature-to-dev PR
contract, and trusted pull-based local delivery from successful main artifacts.
The host has no Actions runner. Releases are isolated outside the coding checkout;
activation checks exact SHA, database and embeddings, with previous-release
rollback. Dependencies and migration changes require explicit host preparation.

Anthropic is failing with exhausted credits. The installed Anvil MLX engine
produced a valid live observation through a tools-free subprocess bridge. Anvil
remains an opt-in fallback; malformed output/timeouts must preserve queue retries.
See docs/DELIVERY.md for configuration, deployment state, and operational limits.

Local verification: 441 passed, 2 skipped, one existing embedding-library warning.
Live Anvil observation and lesson probes passed; lesson inference took 14.75s.
CODE_REVIEW preceded TEST; corrected rollback bootstrap identity verification and
bounded subprocess output buffering. Work is tracked by GitHub issue #71.

Server protection now requires quality/integrity on dev and quality/integrity/drift
on main, strict and enforced for administrators. Actual launchd failure injection
verified that an unstartable candidate is rejected and the committed bootstrap
release restores database/embedding readiness. Integration PR: #72.
