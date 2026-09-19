# Handoff

## State (2026-09-19)

**Both repos reconciled to `dev` and pushed. Two PRs open to `main`, awaiting your merge.**

### agent-memory
- `dev` = `e11fc49`. CI: **271 passed, 1 skipped, 1 xfailed**.
- **#55 released from hold** — the HF Jobs work is squashed onto dev (3 checkpoint commits folded in).
- **MCP fixed.** `.mcp.json` had `${CLAUDE_PLUGIN_ROOT}` in the `command` field. Claude Code
  posix_spawns that string with **no shell**, so it failed ENOENT and agent-memory was dead in
  *every* project (the marketplace is a directory source pointing at this repo). Now an absolute
  path, guarded by `tests/test_mcp_manifest.py` (seen RED first).
- **ADR 0001 amended** — its stated mechanism was wrong ("resolves correctly", "passed to the
  shell"). Corrected in place as a superseding block.
- PR **#53** (dev→main) updated with all of the above.

### agent-hooks
- `dev` = `6720583`, PR **#10** open (dev→main, 6 commits).
- New: **iron-rules** (rule reminders, every 10 turns) and **self-update** (notify-only git
  updater). **hf-launch-gate** was untracked — now committed.
- Tests: iron-rules 12/12, self-update 14/14, hf-launch-gate 25/25.

## Next
1. **Merge PR #10** (agent-hooks) and **PR #53** (agent-memory) — both dev→main, human gate.
2. **Restart Claude Code** to pick up the MCP fix and the new hooks (MCP connects at session start;
   this session still shows the old failure).
3. Anvil reconcile + PR — an agent was running this at session end; check its result.

## Open issues filed today
- agent-hooks **#11** — transcript full-read per turn (276MB RSS), class defect in 2 hooks.
  context-primer.js still has the *tool-result counting* bug that iron-rules fixed.
- agent-memory **#56** — `.mcp.json` absolute path is a release blocker for third-party installs.
  Candidate fix `./scripts/run_mcp.sh` works when cwd=plugin root, but that guarantee is unverified.
- agent-memory **#57** — v5 model leaks real secret-file paths from training data. **Security.**
- agent-memory **#58** — v5 model over-emits tool calls on 60/60 trials; parse-rate metric hides it.

## Context
- **Use `.venv-finetune`** for all Python here (`.venv` is a corrupted Dropbox mirror).
  `pytest-asyncio` was missing from it — installed; that's what made 72 tests "error".
- **`gh auth status` is NOT git's push identity.** osxkeychain credential.helper is separate;
  run `gh auth setup-git` if a push 403s.
- v5 model is at **`WFCA/qwen3-4b-toolcalls-v5-pilot`** (org, not WFCAMZ), quant **Q4_K_M** (not
  Q6_K). Test via LM Studio — runbook in agent-memory notes. Scores 60/60 parse but see #57/#58.
- Do NOT kill the git-session checkpoint hook; squash its commits during reconcile.
