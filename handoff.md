# Handoff — agentMemory

Updated 2026-10-07 19:52 PDT (pre-reboot).

## State

Branch `codex/fix-hf-namespace-comment`, clean, **2 commits ahead of
`origin/dev` and 12 behind — NOT pushed.** Nothing is lost on reboot;
nothing has landed either.

- `4a7ecc2` — `run_hf_job.sh:220` comment had the 402 **backwards**. It
  claimed personal WFCAMZ had no Jobs credits and to set
  `JOB_NAMESPACE=WFCA` to bill the org; the truth is the reverse.
  Evidence: `hf jobs ps -a` lists 2 jobs on personal; `--namespace WFCA`
  returns "No jobs found". That comment was the source of ~3 sessions of
  wrong launch advice.
- `b9a7ef6` — **#57 root-caused and fixed.** See below.

## #57 — path leakage, fixed at the cause

MECHANISM: `scripts/fine_tune/build_v5_pilot_dataset.py` carried a
hand-copied fallback redactor with only `SECRET_PATTERNS`. `PATH_PATTERNS`
was added to `app/redact.py` and never copied to the stub, so any build
where `app.config` can't load (no `.env`, e.g. CI — the stub's own comment
names that case) wrote unscrubbed absolute home paths into the training set.

MEASURED on `datasets/v5_pilot/train.jsonl`, the actual file v5 trained on:
5000 rows, **583 leaking → 0** after the current redactors
(434 home paths / 135 secret-file refs / 31 shell substitutions).
**8.7% of the corpus carried the operator's home path.**

FIX: `app/redact_patterns.py` holds all 22 patterns with **no config
dependency**. `app/redact.py` imports them and applies the settings gates;
the builder imports the same lists and on fallback applies every pattern
ungated. Two copies became one — drift is now unrepresentable, not
remembered. Swept repo-wide: exactly one definition of each list remains.

GUARDS: `tests/test_redact_pattern_single_source.py`, 9 tests, **seen RED
first** (the fallback leaked `/Users/mz` verbatim). Covers config-free
import, single-definition assertion, fallback scrubbing, and that
relative/system paths are **preserved** (over-scrubbing would gut the data
to no benefit).

Suite: **434 passed.** 2 failures are pre-existing and unrelated — missing
`sentence_transformers`, and a stale DB-count fixture in
`test_match_lessons` (`assert 272 in [42,43]`). Both verified
failing/skipped at baseline with my changes stashed.

## Next — in order

1. **Push and PR `codex/fix-hf-namespace-comment` → `dev`.** It is 12
   behind; rebase on `origin/dev` BEFORE `gh pr create`. Contract forbids
   pushing to either trunk directly.
2. **Then decide the retrain.** The free verification is now done and it
   says the run is worth paying for: the fix reaches the training file,
   measured. Two hazards remain, both must be settled before launch:
   - **`HUB_MODEL_ID` defaults to the EXISTING model**
     (`train_hf_job.py:75` → `WFCA/qwen3-4b-toolcalls-v5-pilot`) and the
     push is `create_repo(..., exist_ok=True)` + `update_repo_settings`
     (lines 418-423). **A rerun OVERWRITES the artifact currently working
     in LM Studio.** Set a NEW id (e.g. `…-v6-scrubbed`) to keep a
     known-good fallback.
   - **The job pushes a LoRA adapter ONLY** (line 39), no merged weights
     and no GGUF. Getting it into LM Studio needs the separate
     `ac5_gguf_pipeline.sh` → Q4_K_M step. A COMPLETED job is NOT a
     usable model — that was the explicit requirement.
3. **Retrain scope is narrower than when it was requested.** #58's
   over-emission turned out to be a missing stop sequence — fixed, free,
   verified 0/59 → 59/59 exactly-one. So the retrain now buys **only** the
   #57 path scrubbing. Real, but data hygiene, not capability.
4. **#57 published weights still leak** even with source fixed. Decide
   retract vs supersede for `WFCA/qwen3-4b-toolcalls-v5-pilot`.

## Budget

WFCA $4.00 + WFCAMZ $5.13 = **$9.13** ≈ 11 L4-hours at $0.80/hr billed
per minute. A ~3h run ≈ $2.40. Two runs affordable, but only **one is
certain**: the org's $4 can't be spent until the 402 cause is found, and
that cause is still unestablished. Launch WITHOUT `JOB_NAMESPACE` to use
proven-working personal WFCAMZ.

## Environment gotchas

- **Use `.venv-finetune` for ALL python here.** `.venv` is a corrupted
  Dropbox-mirror copy (pip installs to Dropbox, python reads local prefix).
- **The agent-memory MCP failed to connect this session** —
  `ENOENT … '${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp.sh'`. The repo is
  FINE: the portable `sh -c` fallback form is correct on
  `codex/fix-hf-namespace-comment`, `dev` AND `main` (verified all three).
  The error is stale — a cached/installed plugin copy, or a session
  launched before the fix landed. **A restart should clear it**; if it
  doesn't, hunt the installed copy, not the repo.
- A `PreToolUse` secret guard blocks shell `grep` of `.env`-ish patterns
  even when scanning a dataset rather than reading a dotfile. Count in
  Python instead of fighting it.
- `docs/plans/dataset-factory.md` edits are gitignored.
- Do NOT kill the `git-session` checkpoint hook; squash its commits in
  reconcile.

## Contract reminder

Feature branch → PR to `dev`; `dev` → PR to `main`. Never push a trunk
directly (`enforce_admins=true`, so admin pushes are rejected, not
silently allowed). No `Co-Authored-By` / "Generated with" / robot emoji in
commits — a pre-push hook blocks them (§6).

Three stale PRs still need a human decision, all feature-branch→main,
which the contract forbids: agent-hooks #12, agent-memory #54 and #44.
