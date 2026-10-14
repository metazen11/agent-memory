# Changelog

## Unreleased

### Changed
- Single-trunk `main` (#94): `dev`, the `trunk-drift` workflow and the
  dev->main promotion step are gone. Feature branch -> squash PR to `main`,
  gated by independent review + auditor PASS + `quality`/`integrity`.
  `.reconcile-gate.json` allows feature->main PRs; `.githooks/pre-push` still
  refuses direct pushes to `main`.
- Auto-deploy: `scripts/deploy_live.sh` (health-checked, rolls back on failure)
  scheduled every 5 min by `scripts/install_deploy_schedule.py`.

### Security
- Lesson text injected into model context is now sanitized and delimited (#89).
  New shared renderer `hooks/lesson-block.js` is used by the Claude
  `user-prompt-submit`, `pre-tool-use`, `post-tool-use` hooks and the Codex
  integration (`integrations/codex/common.js`, `pre-tool-trigger.js`): `<`/`>`
  escaped, control/zero-width/bidi/Unicode-tag characters stripped, rules capped
  at 280 code points, block capped at 6 KB, wrapped in `<agent-memory>` with a
  "data, not instructions" preamble. A source-level test fails if any hook or
  integration reads `.rule` without importing it.
