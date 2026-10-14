# CI/CD and enrichment contract

Delivery follows `feature branch -> main` (single trunk, squash-only). The PR
requires `quality` and `integrity`, an independent code review, and an
`aa_auditor` PASS against the originating issue. GitHub protection enforces the
named checks with up-to-date branches and applies to administrators. Review
happens before testing.

`ci-quality.yml` uses a disposable PostgreSQL/pgvector service and an isolated
API. It runs changed-file Ruff with versioned explicit rules, Python/Node syntax checks, and the complete test
suite. The embedding model is the standard 768-dimensional MPNet model, avoiding
remote Python execution and proprietary model credentials. Anvil and Anthropic
are disabled in CI; subprocess failure tests exercise their adapter boundary.
Successful push runs package the exact tested commit with SHA/checksum manifest.
PR runs cannot publish releases or access host credentials.

Two extra gates support the single trunk:

- `agent-gate.yml` (`pull_request_target` + `issue_comment`, no PR checkout) posts
  the commit status `agent-gate`: success only when issue comments carry
  `review: APPROVE <head_sha>` and `audit: PASS <head_sha>` for the CURRENT head
  SHA (`scripts/agent_gate.js`, unit-tested in CI). It becomes a required check
  via branch protection after this workflow is on `main`.
- `protection-drift.yml` runs daily and fails if `main` protection loses
  `enforce_admins`, linear history, strict checks, force-push/deletion denial, or
  required checks `agent-gate`, `integrity`, `quality`, or if the repo allows
  merge commits or rebase merges.

## Local production delivery

**The live service MUST run from the releases directory produced by
`deploy_release.py`, never from a git checkout.** A checkout-backed service has
unpinned dependencies and bypasses the green-CI, checksum, lock, migration-approval
and rollback checks below.

This public repo has no self-hosted Actions runner. A trusted macOS login service
polls every 120 seconds for a successful **push** run of `ci-quality.yml` on the
current `main` SHA. It downloads that run's artifact, verifies its identity and
checksum, and extracts it under `~/.local/share/agent-memory/releases/`. Production
never runs the editable coding checkout after first promotion. Python dependencies
are reused from the prepared host virtualenv; credentials live in private
`runtime.env` (mode 0600), outside Git and artifacts.

One-time bootstrap, from a coding checkout at current `main`. The delivery
controller runs under whichever interpreter runs the installer
(`sys.executable`), so use a working Python 3.12 venv (`.venv-finetune` on this Mac):

```sh
.venv-finetune/bin/python scripts/deploy_release.py --prepare
.venv-finetune/bin/python scripts/install_delivery_service.py
```

**Replacing the checkout-backed API on :3377.** The release-backed API is launchd
job `com.metazen.agent-memory-api` (installed by the first deploy); the old
checkout-backed job is `com.metazen.agent-memory-server` (runs
`~/_CODING/agentMemory-live`). Both bind :3377, so retire the old one first:

1. `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.metazen.agent-memory-server.plist`
2. Move that plist out of `~/Library/LaunchAgents/` (so it cannot reload at login).
3. Let the delivery controller deploy (next 120 s poll, or run
   `.venv-finetune/bin/python scripts/deploy_release.py` once), which loads
   `com.metazen.agent-memory-api` from the release.
4. Verify: `launchctl list | grep agent-memory` shows `-api`, not `-server`; its
   program path is under `~/.local/share/agent-memory/releases/`; and
   `curl -s http://127.0.0.1:3377/api/health` reports `release_sha` equal to
   `git rev-parse origin/main`.

The controller is copied outside the checkout so later source edits cannot change
its behavior. Reinstall it after an intentional controller update. The GitHub CLI
must be authenticated with repository read and deployment-write access. The job
runs while this user's login session is active, as do the API/recovery services.

Deployment reinstalls the supervised API and recovery jobs from the isolated
release. Readiness requires database and embeddings plus the candidate's exact
`release_sha` in `/api/health`. Failed activation reinstalls the previous release
and checks readiness. Deployment status is recorded in GitHub Deployments and
`~/.local/share/agent-memory/deployment.json`; logs are in `delivery.log` there.
A process lock prevents concurrent deployments. Failed deployments are retried
on the next poll, while the previous release remains the recorded active one.

Dependency or SQL migration changes fail closed before production activation:
a matching prepared runtime is required, and editing or removing an applied
migration is always refused. A release that adds migrations deploys only after
the operator reviews them and runs, from a checkout at the target `main`:

```sh
.venv/bin/python scripts/deploy_release.py --approve-migrations
```

This takes a `scripts/backup.sh` dump, then pins each new migration's SHA-256 in
`approved-migrations.json`; changed content needs re-approval. Rollback cannot
undo an irreversible database migration; the dump is the recovery path. The initial
bootstrap release is a committed snapshot, preserving a stable rollback target.
Application startup now fails if migrations fail, rather than serving a partially
migrated database. Anthropic billing degradation does not block durable capture
readiness, and remains visible separately in health.

## Anvil inference fallback

The Anthropic failure observed on this host is exhausted account credits.
Software cannot replenish those credits. Set `ANVIL_FALLBACK_ENABLED=true` in the
private runtime configuration to use the installed Anvil inference engine as a
fallback. Defaults: `ANVIL_ROOT=/opt/anvil`, `ANVIL_TIMEOUT_SECONDS=60`.

The bridge invokes Anvil's Python runtime and `chat_completion` directly, with
**no tools**, agent runner, hooks, memory recursion or raw interaction logging.
It redacts the input, validates returned JSON and records provider identity.
Fallback failures degrade health explicitly. Daemon proxying is disabled so process termination also stops inference. Only one inference subprocess runs at a time. A timeout kills its entire process
group. Invalid output and invocation failures raise a retryable error for queue
observations. Intentional `skip:true` remains a skip. Lesson synthesis tries
Anthropic, then Anvil, then local GGUF; the existing rule-quality validator still
applies. Observation generation retains local GGUF first, then Anthropic, then
Anvil. Provider provenance appears in observation `raw_text` (excluded from embedding input), lesson
`synthesized_by`, and `/api/health` under `llm.anvil_fallback`.

The installed `anvil run` CLI advertises file/shell tools even in its minimal
preset, so it is unsuitable for automated memory enrichment. The command-line
bridge reuses the same inference engine without enabling those capabilities.
Live model quality and latency must be tested on each host before enabling it;
CI verifies the process boundary without downloading the host's MLX model.
