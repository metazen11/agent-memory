# CI/CD and enrichment contract

Delivery follows `codex/* -> dev -> main`. Both PRs require `quality` and
`integrity`; promotion to main also requires `drift`. GitHub protection enforces
named checks with up-to-date branches and applies to administrators. Review
happens before testing; authorization to promote is recorded in the task.

`ci-quality.yml` uses a disposable PostgreSQL/pgvector service and an isolated
API. It runs changed-file Ruff with versioned explicit rules, Python/Node syntax checks, and the complete test
suite. The embedding model is the standard 768-dimensional MPNet model, avoiding
remote Python execution and proprietary model credentials. Anvil and Anthropic
are disabled in CI; subprocess failure tests exercise their adapter boundary.
Successful push runs package the exact tested commit with SHA/checksum manifest.
PR runs cannot publish releases or access host credentials.

## Local production delivery

This public repo has no self-hosted Actions runner. A trusted macOS login service
polls every 120 seconds for a successful **push** run of `ci-quality.yml` on the
current `main` SHA. It downloads that run's artifact, verifies its identity and
checksum, and extracts it under `~/.local/share/agent-memory/releases/`. Production
never runs the editable coding checkout after first promotion. Python dependencies
are reused from the prepared host virtualenv; credentials live in private
`runtime.env` (mode 0600), outside Git and artifacts.

Preparation and installation, from the coding checkout:

```sh
.venv/bin/python scripts/deploy_release.py --prepare
.venv/bin/python scripts/install_delivery_service.py
```

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
a matching prepared runtime and an explicit migration compatibility plan are
required. Rollback cannot undo an irreversible database migration. The initial
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
Anvil. Provider provenance appears in observation raw JSON, lesson
`synthesized_by`, and `/api/health` under `llm.anvil_fallback`.

The installed `anvil run` CLI advertises file/shell tools even in its minimal
preset, so it is unsuitable for automated memory enrichment. The command-line
bridge reuses the same inference engine without enabling those capabilities.
Live model quality and latency must be tested on each host before enabling it;
CI verifies the process boundary without downloading the host's MLX model.
