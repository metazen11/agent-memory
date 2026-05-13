---
name: Fine-tune pipeline phase
about: A single phase of the fine-tune pipeline (M-FT-N). Use one per pipeline phase.
title: 'M-FT-N-X: <phase short name>'
labels: 'fine-tune, phase'
---

## Phase ID

`M-FT-<milestone>-<phase letter>` — must match the canonical id in `docs/fine_tune/PIPELINE_RUNBOOK.md`.

## Objective

What this phase produces and why. One sentence. Reference the **business/user outcome** from the parent milestone — for fine-tune work the outcome is always "GGUF that emits parseable Hermes tool calls and renders correctly in LM Studio."

## Inputs

- Canonical paths and files this phase reads.
- Upstream phase that must complete first (e.g. `Phase N-1 must produce models/base/<slug>/REVISION.txt`).
- Env vars / config that influence behavior.

## Outputs

- Canonical paths this phase writes (use exact names from `scripts/fine_tune/lib.py:MODELS`).
- Manifest / report files (SHA256 sidecars, `MANIFEST.json`, etc).

## Pass criteria (objective, scriptable)

- [ ] criterion 1, written as a check (e.g. "validator exits 0 with parse_rate >= 0.8")
- [ ] criterion 2
- [ ] criterion 3

If any criterion isn't scriptable, restate it until it is. "Looks good" is not a criterion.

## Commands

```bash
# The exact command sequence that runs this phase, copy-pasteable.
.venv-finetune/bin/python scripts/fine_tune/<script>.py <args>
```

## Failure modes considered

- Mode 1 → handled by ...
- Mode 2 → handled by ...

Reference `docs/fine_tune/FAILURE_MODES.md` for known patterns.

## Security / data

- PII or secret handling specific to this phase (or "N/A — phase touches no user data").
- Anything written to disk that must be scrubbed.

## Observability

What gets logged where; how to verify after the fact (e.g. "tail `logs/m-ft-1/<phase>_*.log`").

## Rollback

Single sentence: how to undo this phase if it produced bad output. For training: "previous adapter still at `latest`; current run is in `runs/<UTC>/`; delete the run dir to revert."

## Reproducibility

- Seed: `42` (or N/A).
- Environment: `.venv-finetune` (pin if it's been rebuilt).
- Base model revision: pinned in `models/base/<slug>/REVISION.txt`.
- Dataset version: `v1` (or whatever).

## Acceptance

The phase is **done** when:
1. All pass criteria above are checked.
2. Outputs exist at their canonical paths.
3. Logs are saved.
4. Docs updated if the phase changes the canonical recipe (`docs/fine_tune/PIPELINE_RUNBOOK.md`, `FAILURE_MODES.md`).
5. This issue is closed referencing the commit that delivered it.
