# Data Quality Gates for Fine-Tuning

**Why this doc exists:** v4 shipped with 100% multi-turn adaptation but
still rewrote user-provided paths (`/Users/mz/_CODING/anvil` →
`/Users/mz/Dropbox/_CODING/anvil`) at inference time. Root cause: 78%
of training rows had Dropbox-rooted cwds because that's where the user
historically worked. The model learned that prefix as the "default"
project path. Migration 014 fixed the data; this doc ensures we never
re-introduce that class of bug.

## The core principle

> **The model will memorize the most-frequent surface patterns in the
> training data, including patterns the dataset author didn't intend
> as targets. Audit for those patterns BEFORE training, not after.**

A model trained on data where 78% of paths start with `/Dropbox/_CODING/`
will rewrite user-typed `/Users/mz/_CODING/X` into `/Users/mz/Dropbox/_CODING/X`
even at temperature 0.0 with an explicit "do not fabricate paths"
system prompt. The bias is in the weights now; you can't prompt it out.

## The audit categories (run before every training run)

### Category 1: Path bias

**Threat:** Model memorizes stale/wrong paths and rewrites user input.

**Audit checks** (run against `train.chat.jsonl` after dataset build):

| Check | Threshold | Why |
|---|---|---|
| % of rows with `Dropbox/` substring | < 1% | Per CLAUDE.md, local `_CODING/` is source of truth; Dropbox is stale mirror |
| % of rows referencing a single project root | < 30% per project | No one project should dominate path completion |
| % of rows with absolute paths under `/Users/<other-name>/` | < 1% | Foreign-user paths in training = model fabricates them |
| Mismatch: user prompt path prefix ≠ tool_call path prefix | < 5% of rows | If the user types `/_CODING/X` and the assistant calls `/Dropbox/_CODING/X`, the row teaches contradiction |

**Remediation:** path-rewrite in DB (migration 014 pattern), then
rebuild dataset. NOT a builder-side filter — the data itself is wrong.

### Category 2: Scaffold token leakage

**Threat:** Stray `<tool_call>`, `<|im_start|>`, `</tool_call>` tokens
inside tool arguments confuse the chat-template tokenizer.

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| Rows with chat-template tokens inside `tool_input` | 0 | Already covered by Fix #1 in build_v3_dataset.py |

**Existing implementation:** `_strip_chat_scaffolding_inplace()` in
build_v3_dataset.py. Re-verified at row-build time.

### Category 3: Zero-predicted-token rows

**Threat:** Rows whose assistant span tokenizes to zero non-special
tokens under MAX_LENGTH produce NaN loss in `CrossEntropyLoss(ignore_index=-100)`
at batch_size=1.

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| Rows producing 0 predicted tokens at MAX_LENGTH=1024 (full tier) | 0 | NaN at first eval pass |
| Same at MAX_LENGTH=512 (tiny tier) | 0 | Tiny smoke test would crash |

**Existing implementation:** Fix #10 in build_v3_dataset.py; verified
in builder + preflight.sh + trainer assertion (three layers).

### Category 4: Empty-args with required parameters

**Threat:** Model learns to emit `{"arguments": {}}` for tools that
require parameters → empty-args loop bug (v1 regression).

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| Rows where tool has `required` params but `arguments == {}` | 0 | Direct teaching of the v1 bug |

**Existing implementation:** `_is_empty_args_problematic()` in
build_v3_dataset.py.

### Category 5: Mutation actions on home repo

**Threat:** Model learns to emit `gh issue create` / `git commit` /
`npm publish` as the FIRST tool_call when asked about this repo.
Off-distribution for the intended discovery use case.

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| First-tool-call rows that are mutations on prompts mentioning this repo | < 1% of total | Teach discovery first, not mutation |

**Existing implementation:** Fix #5 in build_v3_dataset.py.

### Category 6: Vision rows / subagent transcripts / task notifications

**Threat:** Off-distribution content the v4 use case shouldn't train on.

**Existing implementation:** Fixes #4, #8, #9 in build_v3_dataset.py.

### Category 7: Repetition cap

**Threat:** Tool_inputs containing repeated lines (often from `git log`
or `find` output stuffed into Bash args) teach the model to emit
hundreds of duplicate lines.

**Existing implementation:** Fix #7 in build_v3_dataset.py.

### Category 8a: Agentic-narrative fabrication (v4.5 addition — CRITICAL)

**Threat:** Model generates plausible-sounding but fabricated agentic
narrative when it doesn't know what to do. Example v4 output in real
production use:

> Let me check what's actually open: GitHub has #309 (PR #312) open
> now for a different change. [...] Let me create `feat/airbyte-integration`
> from this branch [...] Plan: 1. Branch off current state. 2. Add all
> 6 connector files. 3. Commit → `commit -m "feat(connectors): Airbyte
> connector management (30 files)"`. [...] Want me to proceed?

Every PR number, branch name, file count, commit hash here is fabricated.
The model never ran a single command in this monologue — it generated
the SHAPE of an agentic-planning response from training-distribution
patterns.

**Source in training data:**
- `mem_observations.narrative` — long meta-discussions of "what to do
  next" that read like agent monologue
- `mem_tool_calls.tool_input` for tool=Bash — heredocs containing
  drafted commit messages, PR bodies, multi-step plans
- `mem_user_prompts.prompt_text` — when the user pasted long planning
  text as a prompt

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| Rows where assistant text contains "Let me " 3+ times | drop | Agentic-monologue shape |
| Rows where assistant text contains "Want me to proceed" or "Want me to" | drop | Fabricated-planning shape |
| Rows where tool_response_preview contains "PR #<digits>" without a real ref | flag for human review | PR numbers in tool outputs are hard to verify, drop bulk |
| % of rows whose assistant text is > 600 chars and contains no tool_call | < 10% | Long text-only assistant turns dilute tool-use training |
| Rows referencing commit hashes that don't exist | flag | Invented git state |

**Remediation:** filter at the builder. Patterns are easy to match; the
hard part was noticing them. They went unnoticed in v3/v4 because the
existing filters focused on TOOL behavior (empty args, scaffold tokens,
repetition) not on TEXT behavior.

**Implementation:** see `scripts/fine_tune/audit_dataset.py` Category 8a.

### Category 8b: Multi-turn coverage (v4 addition)

**Threat:** Model never sees `tool → assistant(text)` transitions →
can't ground tool_responses (the v3 regression).

**Audit checks:**

| Check | Threshold | Why |
|---|---|---|
| % of train rows with `len(messages) == 5` (multi-turn) | ≥ 20% | v3 was 0%, v4 is 40% |

**Existing implementation:** `_extend_to_multi_turn()` in
build_v4_dataset.py + preflight.sh multi-turn gate.

## The pre-training audit script

`scripts/fine_tune/audit_dataset.py` runs ALL categories above against
a built dataset and produces a PASS/FAIL report. Integration:

1. `build_v{N}_dataset.py --write` produces the dataset.
2. `scripts/fine_tune/preflight.sh <slug> <family> <version>` runs the
   audit script AND the existing zero-label gate.
3. Trainer refuses to launch if preflight fails.

The audit is **mandatory** for full-tier runs. Skip with
`SKIP_AUDIT=1` only for tiny smoke tests.

## Process for adding a new audit category

When a training run exposes a new class of bias:

1. **Write a postmortem** at `docs/training_runs/v{N}-incident-<date>.md`
   describing the failure mode + how it was diagnosed.
2. **Add a check** to `scripts/fine_tune/audit_dataset.py` with a
   threshold + remediation note.
3. **Update this doc** with the new category.
4. **If the failure is in the source data, not the dataset build:**
   write a migration (like 014) and a write-side guard so new data
   doesn't reintroduce it.

## v4 path-bias postmortem (2026-05-18)

**Symptom:** v4 in production. User types
`can you switch into the project /Users/mz/_CODING/anvil`. Model emits
`workspace_switch({"path": "/Users/mz/Dropbox/_CODING/anvil"})`.
At temperature 0.0. With explicit system prompt telling it not to
fabricate paths.

**Diagnosis:**
- mem_tool_calls.cwd: 67,755 / 86,646 (78%) start with `/Dropbox/_CODING/`
- mem_tool_calls.tool_input: 37,799 / 86,646 (44%) contain `/Dropbox/`
- mem_projects: 92 / 826 (11%) of canonical roots are Dropbox
- mem_projects had duplicate rows for the same project (one Dropbox,
  one local) — recall queries fragmented

**Root cause:** Pre-2026-04 work happened under `~/Dropbox/_CODING/`.
After moving to local SSD (per CLAUDE.md), the memory tables kept the
old paths. Training data inherited the bias.

**Why prompt mitigation failed:** Prefix bias in training is too strong
for system-prompt instructions to overcome. The model has learned
"agentMemory's path SHAPE starts with Dropbox" as a statistical fact.

**Fix:** Migration 014 normalized 248,177 column-rows across mem_*
tables. Merged 15 duplicate project rows. Added write-side normalizer
(task #28). Future training uses clean data.

**Prevention:** This audit gate (Category 1 above).

## References

- `scripts/migrations/014-normalize-dropbox-paths.sql` — the fix
- `scripts/migrations/014-normalize-dropbox-paths.down.sql` — rollback
- `migration_014_path_backup` table — 248k rows backed up for rollback
- `scripts/fine_tune/preflight.sh` — full preflight gate
- `scripts/fine_tune/audit_dataset.py` — pre-training audit (to be added)
- `~/CLAUDE.md` — "Source of truth: ~/_CODING/, Backup mirror: ~/Dropbox/_CODING/"
