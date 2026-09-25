# Handoff

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
