# Design — Methodology-conditioned dataset ("ingrain the methodology")

**Status:** DESIGN / REQUIRES-REVIEW. This document designs a dataset builder;
it does **not** build it. No script here should be run against training until
the interface below is reviewed and approved.

**The deeper goal.** Today the engineering methodology (verify-end-state,
no-secrets, root-cause-before-fix, rebase-before-PR, …) lives in *prompt packs
and PreToolUse hooks* — it is harness-enforced. The user wants that quality
baked **into the weights**, so the model *is* a methodology-following engineer
even when a rule isn't injected. That means two additions to the existing
project-conditioned v5 dataset:

1. **Positive demonstrations of the critical lessons** — turn each lesson into
   training rows where the model does the *good* behavior the lesson prescribes.
2. **Curated good tool-call traces** — keep only gate-passing,
   methodology-following sequences; discard the rest. *Curation is the whole
   game* (the user's own memory flags v2 linkage breakage — a dataset that
   includes broken/bad traces teaches the broken behavior).

This composes with, and does not replace, the v5 project-conditioning.

---

## How lessons enter training data today (grounding)

Two mechanisms already exist in the repo — understand both before extending:

1. **Lessons as captured tool-calls.** `build_v2_dataset.py` and
   `build_v3_dataset.py` have schemas + descriptions for
   `mcp__agent-memory__create_lesson` (and `save_memory`, `search`, etc.). When
   a past session *created a lesson*, that `create_lesson` tool-call is a normal
   row in `mem_tool_calls` and flows into the dataset like any other tool-call.
   So the model currently learns "*how to file a lesson*", **not** "*how to obey
   one*". That is the gap this design closes.

2. **Lessons as a live table.** `mem_lessons` (verified live: **133 rows**) has
   columns `title, rule, severity, trigger_tool, trigger_pattern, trigger_on,
   trigger_phase, active, raw_text`. This is the authoritative, structured
   source of the methodology — the same rows the PreToolUse hook injects at
   runtime. **This table, not the captured `create_lesson` calls, is the source
   for methodology rows.**

Everything below reads `mem_lessons` (and `mem_tool_calls` for traces) through
`scripts/psql_wrapper.sh` — the only sanctioned DB path — exactly as
`build_v5_pilot_dataset.py` already does.

---

## Part 1 — Lessons → positive-demonstration rows

**Principle:** a lesson is a *rule*; a training row must be a *demonstration of
obeying the rule*, not a recitation of it. Recitation teaches the model to talk
about the rule; demonstration teaches it to act correctly. We generate, per
critical lesson, one or more (prompt → correct action) rows.

### Which lessons (curation of the source)
Not all 133 lessons belong in the weights. Select by:
- `active = true` AND `severity IN ('critical','warning')` (skip `info` noise).
  NOTE: `mem_lessons.severity` uses `critical`/`warning`/`info` — **not**
  `high`/`critical`. Filtering on a literal `"high"` matches **zero rows** (this
  was OQ-1). Live counts: `critical` **101**, `warning` **32**, `info` **1**;
  the selected set is `critical`+`warning` = **133 rows** (decision of 2026-07-07).
- The builder MUST **fail closed**: if the lesson query returns 0 rows, abort the
  build with a non-zero exit rather than silently producing an empty Part-1 set.
- Prefer lessons whose behavior is **demonstrable as a tool-call sequence**
  (verify-end-state, root-cause-before-fix, rebase-before-PR, no-secrets,
  migrations-are-immutable) over purely advisory ones.
- **Exclude** lessons that are runtime-injectable and volatile (they belong in
  the hook, not the weights) — same reasoning the v5 design uses for *not*
  training the Anvil system prompt or global lessons (HANDOFF "locked-in design
  decisions" #1). A lesson that changes monthly should stay in the hook.

This mirrors the existing v5 rule "runtime-injectable ⇒ don't train it" — we
only bake in the **stable, structural** methodology.

### How a lesson becomes rows (three complementary shapes)

For a lesson like *"verify the end state with evidence, not the action"*:

- **(a) Contrast/preference pair (best signal).** Same triggering prompt, two
  responses: the *chosen* one performs the verifying action (e.g. after a
  migration, a `SELECT` that reads the artifact back / an `EXPLAIN`); the
  *rejected* one stops at "done". This is a **DPO/KTO preference row**, not SFT —
  TRL supports it (`DPOConfig`, chosen/rejected columns) and the
  `huggingface-llm-trainer` skill documents it. Preference data is the most
  direct way to encode "do X, not Y" and avoids teaching the model to narrate
  the rule.
- **(b) Positive SFT demonstration.** A realistic prompt in the lesson's domain
  → assistant turn that takes the correct tool-action the lesson prescribes. No
  mention of the lesson text. Reuses the exact chat-format rows the existing
  builders emit (system + user + assistant tool_call + tool response).
- **(c) Trigger-anchored demonstration.** Use the lesson's `trigger_tool` /
  `trigger_pattern` / `trigger_phase` to construct a row whose context matches
  where the hook *would* fire, and whose assistant response is the compliant
  action. This teaches the model to self-trigger the behavior in the same
  situations the hook watches — the point being that a hook miss still yields
  correct behavior.

**Provenance, not fabrication.** Where possible, demonstrations are drawn from
**real** gate-passing traces (Part 2) that already exhibit the behavior, tagged
to the lesson — not synthetically written. Synthetic rows are a fallback for
lessons with no real positive example, and are marked `synthetic=true` (the
existing builders already carry a `synthetic` flag) so their share can be capped
and audited.

---

## Part 2 — Curate good tool-call traces (quality filter)

**Principle:** only gate-passing, methodology-following sequences may enter the
set. This extends the filters the existing builders already apply.

### Existing quality filters to reuse (do NOT reinvent)
`build_v3_dataset.py` already implements, and this builder should compose with:
- empty-args / missing-schema drops (v1 loop-bug shape),
- off-distribution mutation drop for this-repo prompts (fix #5),
- in-args repetition + length caps (fix #7),
- vision / subagent / task-notification drops (fix #4/#8/#9),
- the assistant-span predicted-tokens mask gate (fix #10),
- per-tool caps + session-aware split.

`build_v5_pilot_dataset.py` adds defensive secret redaction + `<TRUSTED_ROOT>`
path normalization + project conditioning. **All of that stays.**

### New methodology-quality filters (the curation layer)

Verified against the live schema — `mem_tool_calls` exposes `tool_error`
(NULL ⇒ the call succeeded) and `queue_status`; there are **28,599**
`backfill_jsonl` rows to curate from. Filters:

1. **Gate-pass only.** Drop any trace whose tool-call ended in error:
   `tool_error IS NOT NULL` → drop. (The v5 builder already requires
   `tool_error IS NULL` in its source query — keep that, and extend to the whole
   sequence: if *any* step in a session's kept sequence errored, treat the
   sequence as tainted.)
2. **Methodology-followed sequences.** Prefer sequences that exhibit the good
   pattern, using observable proxies:
   - **verify-after-mutate:** a write/migration/deploy tool-call *followed by* a
     read-back (a `SELECT`/`EXPLAIN`/`curl`/`Read` of the thing just changed) in
     the same session → high-value, keep + tag `methodology:verify-end-state`.
   - **root-cause-before-fix:** a diagnostic/read step preceding the fix, rather
     than repeated identical fix attempts → keep; a loop of 2+ identical failing
     mutations → drop the loop (anti-pattern).
   - **rebase-before-PR:** a `git fetch`/`git rebase` before a `gh pr create` /
     push → keep + tag.
   - **no-secrets:** already enforced by redaction; additionally drop any trace
     where a secret pattern appeared *unredacted at capture time* (belt: rebuild
     over the redactor and count).
3. **Source-quality gate.** Honor the v2-linkage lesson (memory
   `project_v2_data_findings`): only use rows whose prompt↔tool-call linkage is
   sound. Reuse the existing `prev_user_prompt_id` join (v2/v3) and **drop rows
   where linkage is null/ambiguous** rather than guessing — a mis-linked prompt
   teaches the wrong (prompt → action) mapping, which is exactly the v2 breakage.
4. **Positive-only for methodology.** Where a trace is used as a lesson
   demonstration (Part 1b/c), it must be a *clean* example of that behavior — no
   near-miss "mostly followed the rule" traces. Curation is strict: when in
   doubt, drop.

### Composition with v5 project-conditioning
Every kept row still gets the v5 `[Project]` block (project_name / project_root
/ subfolder / cwd, `<TRUSTED_ROOT>`-normalized). Methodology tagging is an
*additional* axis, orthogonal to the project axis. A row can be both
`project:fire-map` and `methodology:verify-end-state`. Apply **per-methodology
caps** the same way v3 applies per-project caps, so no single behavior dominates
and the mix stays balanced against the project-conditioning signal.

---

## Proposed builder interface (REQUIRES-REVIEW — do not build yet)

A new script, sibling to the existing builders, reusing their helpers:

```
scripts/fine_tune/build_methodology_dataset.py   # PROPOSED — not yet created

Inputs (env / CLI, mirroring build_v5_pilot_dataset.py --config style):
  --config configs/methodology.yaml     # flat yaml, same loader as v5
  --dry-run                             # print funnel + tag histogram, write nothing

Config keys:
  # source
  source_window_recent_rows: 30000      # DB window (via psql_wrapper.sh)
  lesson_severities: ["critical","warning"]  # mem_lessons.severity values (NOT "high" — see OQ-1)
  lesson_active_only: true              # mem_lessons.active = true
  lesson_abort_on_empty: true           # fail closed if the lesson query returns 0 rows
  exclude_volatile_lessons: true        # skip runtime-injectable lessons

  # methodology shapes
  emit_preference_pairs: true           # Part 1a — DPO chosen/rejected rows
  emit_positive_sft: true               # Part 1b/c — SFT demonstrations
  prefer_real_traces_over_synthetic: true
  max_synthetic_pct: 0.15               # cap fabricated demos

  # trace curation
  require_gate_pass: true               # tool_error IS NULL across the sequence
  drop_mislinked_prompts: true          # honor v2-linkage lesson
  methodology_tags:                     # observable-proxy detectors to run
    - verify-end-state
    - root-cause-before-fix
    - rebase-before-PR
    - no-secrets
  per_methodology_cap_pct: 0.15         # balance vs project axis

  # composition (reuse v5)
  apply_v5_project_block: true          # [Project] conditioning stays on
  normalize_paths: true                 # <TRUSTED_ROOT>
  redact_secrets: true                  # defensive redaction (always)

Outputs:
  datasets/methodology/train.sft.jsonl      # SFT rows (Part 1b/c + curated traces)
  datasets/methodology/train.pref.jsonl     # DPO chosen/rejected rows (Part 1a) — separate file/split
  datasets/methodology/AUDIT.md             # funnel, per-lesson row counts,
                                            # per-methodology-tag counts, synthetic %,
                                            # gate-pass drop counts, sample rows
```

**Two output splits on purpose.** SFT and preference (DPO) data train
differently. The runbook's Phase 2 trains SFT by default; a preference pass
(DPO) is a *second, later* run on `train.pref.jsonl` once the SFT model is a
sane starting policy — the same two-stage shape TRL/`huggingface-llm-trainer`
recommends. Do not merge them into one SFT file.

### Reuse map (what the new builder imports rather than duplicates)
- **Redaction** → `redact_text` / `redact_json` from **`app/redact.py`** (the
  authoritative module; `build_v5_pilot_dataset.py` imports it, calling it on the
  prompt, `tool_input`, and `tool_response` of every DB-sourced row). It is **NOT**
  in `v5_schema.py` — that file only does path normalization + row rendering.
  ⚠ **GENERATE-inlet hazard:** the DB (SELECT) inlet is redacted *because it flows
  through `build_v5_pilot_dataset.py`*. Any **GENERATE inlet** that constructs rows
  WITHOUT that path would bypass redaction entirely — the exact secret-leak class
  this repo was burned by. The GENERATE inlet MUST call `redact_json` on every
  synthesized field and pass the 0-secrets gate (below) before its rows are written.
- DB access + CSV parse + `<TRUSTED_ROOT>` normalization + `[Project]`
  block → import from `build_v5_pilot_dataset.py` / `v5_schema.py`.
- schema/envelope building, empty-args + repetition + vision filters, mask gate,
  per-tool caps, session-aware split → import from `build_v3_dataset.py`.
- lesson source → new query on `mem_lessons` (structured), NOT the captured
  `create_lesson` tool-calls.

### Open review questions (must be answered before building)
1. **Preference pairs need a credible "rejected".** Where does the rejected
   response come from — a real past failure trace, the base model's own greedy
   output, or a templated bad pattern? Each has bias tradeoffs. **Recommend:**
   prefer real past failures (they're honest negatives); template only as
   fallback, marked synthetic.
2. **Synthetic demonstration risk.** Fabricated "correct behavior" rows can
   encode *our* idea of correct rather than a real one. Cap hard
   (`max_synthetic_pct`) and audit every synthetic row by hand for the first
   build.
3. **Methodology-proxy precision.** "verify-after-mutate" via a following
   read-back is a heuristic; it will have false positives. Sample and hand-check
   the tag before trusting it as a positive label.
4. **Balance.** How much methodology signal vs project signal vs raw tool-call
   competence? Start small (methodology ≤ ~15% of rows) so the pilot still
   primarily learns tool competence, then increase if evals show the
   methodology behaviors improving without regressing tool selection.

---

## Acceptance criteria for the (future) methodology builder

Before any methodology-conditioned run is declared done:
- `AUDIT.md` shows per-lesson and per-methodology-tag row counts; no single tag
  or lesson dominates (per-cap respected).
- Synthetic share ≤ `max_synthetic_pct`; every synthetic row hand-reviewed on
  the first build.
- 0 unredacted secrets (count-only grep) — checked on **both** inlets. The
  GENERATE inlet specifically must route every synthesized field through
  `app.redact.redact_json` and assert 0 secret matches before writing; a nonzero
  count aborts the build (fail closed).
- 0 mis-linked prompt rows (v2-linkage lesson honored).
- The mask gate (v3 fix #10) is green on the output (0 zero-predicted-token rows).
- An eval shows the target behaviors (e.g. verify-end-state) improving vs the
  project-only v5 model **without** regressing cross-project tool selection —
  measured, with evidence, not asserted.

---
---

# Part 3 — The WFCA Dataset Factory (GENERATE + CURATE, one gate, one eval)

**Status:** PLAN / REQUIRES-REVIEW. Extends the design above. Adds the two
layers Parts 1–2 did not cover: a **verifiable-generation inlet** (APIGen/xLAM
methodology) and a **categorized eval harness** (BFCL methodology). No code, no
DB writes, no dataset builds are authorized by this section — it is a reviewable
blueprint. The companion operator-facing plan lives at
`docs/plans/dataset-factory.md` and references this section; this section is the
canonical design, that one is the phased rollout checklist.

## Thesis (why this section exists)

The best public tool-call datasets are worth copying for their **methodology**,
not their rows (see `docs/datasets/tool-call-datasets-shortlist.md` — that doc
lists the *sources*; this section defines the *pipeline*). Three methodologies
we adopt:

- **xLAM / APIGen — verifiable generation.** Every synthesized tool-call passes
  staged validation (format → executes-against-signature → semantic /
  answer-verifiable) *before* it enters the set. This is the direct fix for the
  linkage-breakage and quality-rot that hurt v2/v4 (HANDOFF: v4 cross-project
  hallucination; memory `project_v2_data_findings`: v2 linkage broke). Curation
  (Part 2) removes bad *real* rows; verifiable generation prevents bad *new*
  rows from ever being written.
- **Hermes — the template contract.** Clean system/tools/tool_response
  multi-turn schema. Our model already trains on Hermes/ChatML + the Qwen3
  template via `v5_schema.render_row`. The factory emits **exactly** that shape;
  it does not introduce a second row format.
- **BFCL — categorized eval.** Score by *category* (simple / parallel /
  multiple-tool-choice / irrelevance-abstain), not one aggregate, so we learn
  *which kind* of tool-calling is weak. `eval_harder.py` already has the
  server-harness + deterministic-scoring skeleton; we extend its category set
  rather than build a new harness.

## Grounded facts (verified read-only via `scripts/psql_wrapper.sh`, 2026-07-07)

These numbers gate the design; they are re-verified at Phase 1, not assumed.

| Fact | Value | Source query |
|---|---|---|
| `mem_tool_calls` total | **97,392** | `count(*)` |
| `mem_tool_calls` with `tool_error IS NULL` (candidate curation pool) | **96,180** | `WHERE tool_error IS NULL` |
| `mem_tool_calls` with `tool_error IS NOT NULL` (dropped by gate) | **1,212** | `WHERE tool_error IS NOT NULL` |
| Gate-pass curation source (v5 builder's exact predicate) | **28,658** | `tool_response_preview IS NOT NULL AND tool_error IS NULL AND prompt_text NOT NULL/''` |
| Rows with `tool_input` populated (schema-validatable) | **97,324 / 97,392** | `WHERE tool_input IS NOT NULL` |
| Rows with `session_id` (sequence reconstruction) | **97,392 (100%)** | `WHERE session_id IS NOT NULL` |
| Distinct `tool_name` values | **97** | `count(DISTINCT tool_name)` |
| `mem_lessons` total | **134** | `count(*)` |
| `mem_lessons` critical+warning severity | **133** | `WHERE severity IN ('critical','warning')` (live 2026-07-07) |
| `mem_lessons` with a `trigger_tool` | **76** | trigger-anchored demo candidates |
| `mem_observations` total | **69,209** | `count(*)` |

**OQ-1 — RESOLVED (2026-07-07).** `mem_lessons.severity` uses the values
**`critical` / `warning` / `info`** — *not* `high` / `critical`. The original
Part 1 config key `lesson_min_severity: "high"` matched **zero rows** (silent
empty set). Fixed above: the config now selects `severity IN ('critical','warning')`
and the builder MUST **fail closed** on a 0-row lesson query. Live severity
distribution (whole table): `critical` **101**, `warning` **32**, `info` **1**.
Selected set = `critical`+`warning` = **133 lessons** (user decision 2026-07-07;
the earlier draft's 31/19/1 breakdown was a stale snapshot).

Top tools by volume (for generation coverage + eval realism): `Bash` 40,655 /
`Read` 19,152 / `Edit` 11,390 / `anvil_task_complete` 5,154 / `Grep` 4,303 /
`Write` 3,561 / `Glob` 1,432. The long tail (97 distinct) includes MCP tools
(`read_file`, `bash_run`, `edit_file` are the anvil-side duplicates) — the
generation inlet should focus on the tools with real JSON schemas we can
validate against, i.e. the in-house Claude/Anvil/agent-memory tool set.

## The unified pipeline — one factory, two inlets, one gate, one output, one eval

```
                        WFCA DATASET FACTORY
  ┌──────────────────────────────────────────────────────────────────────┐
  │                                                                        │
  │   INLET A: CURATE (real)            INLET B: GENERATE (synthetic)       │
  │   ─────────────────────             ──────────────────────────         │
  │   mem_tool_calls (97k)              tool JSON schemas (in-house set)    │
  │   mem_lessons (134)                 + project catalog (fire-map,        │
  │   mem_observations (69k)              agentMemory, anvil, real paths)   │
  │        │                                   │                            │
  │        │ Part 2 filters:                   │ APIGen-style sampler:      │
  │        │  gate-pass, methodology-          │  pick tool(s) → sample     │
  │        │  followed, linkage-sound,         │  project-conditioned args  │
  │        │  redact, <TRUSTED_ROOT>           │  from real path/value pools│
  │        │  (build_v3 + build_v5 helpers)    │  → candidate tool_call     │
  │        ▼                                   ▼                            │
  │   ┌───────────────────────────────────────────────────────────┐        │
  │   │        SHARED VALIDATION GATE (APIGen 3-stage)             │        │
  │   │  Stage 1  FORMAT      parse <tool_call>, name+arguments    │        │
  │   │                       present, JSON well-formed            │        │
  │   │                       → reuse parse_tool_calls()           │        │
  │   │  Stage 2  SIGNATURE   arguments validate against the tool's│        │
  │   │                       real JSON Schema (Draft7);           │        │
  │   │                       unknown tool ⇒ reject                │        │
  │   │                       → reuse jsonschema block +           │        │
  │   │                         extend validate_tool_calls.py      │        │
  │   │  Stage 3  SEMANTIC    answer-verifiable checks:            │        │
  │   │           /ANSWER     - referenced paths resolve under     │        │
  │   │                         <TRUSTED_ROOT> project (audit C8a) │        │
  │   │                       - no fabricated hashes/PRs           │        │
  │   │                       - abstain rows: NO tool_call fired   │        │
  │   │                       - sequence: verify-after-mutate,     │        │
  │   │                         no identical-call loop (AntiLoop)  │        │
  │   └───────────────────────────────────────────────────────────┘        │
  │                              │ PASS only                                │
  │                              ▼                                          │
  │   ┌───────────────────────────────────────────────────────────┐        │
  │   │   SHARED OUTPUT: Qwen3-template rows (v5_schema.render_row) │        │
  │   │   + [Project] block + <TRUSTED_ROOT> + provenance tags     │        │
  │   │   {source: curated|generated, synthetic: bool,             │        │
  │   │    methodology_tag, project_tag, gate_stages_passed}       │        │
  │   │   → train.sft.jsonl / train.pref.jsonl (Part 1a) / AUDIT.md│        │
  │   └───────────────────────────────────────────────────────────┘        │
  │                              │                                          │
  └──────────────────────────────┼──────────────────────────────────────────┘
                                 ▼
             ┌───────────────────────────────────────────────┐
             │   BFCL-STYLE EVAL HARNESS (extends             │
             │   eval_harder.py; held-out, never trained on)  │
             │   Categories: simple · parallel ·              │
             │   multiple-tool-choice · irrelevance-abstain · │
             │   (+ retained WFCA: path_bias, cross_project,  │
             │      ood_project, fabrication)                 │
             │   → per-category before/after scorecard        │
             └───────────────────────────────────────────────┘
```

Both inlets converge on **the same gate** and **the same renderer**. That is the
whole point: a curated real row and a generated synthetic row are
indistinguishable downstream except for their provenance tag, and *neither*
enters the set without passing all three gate stages. The gate is the single
quality choke-point — the APIGen insight applied to both real and synthetic data.

## The WFCA twist — project-conditioned generation + dilution guard

Our differentiator (shortlist doc "Design principle"; HANDOFF v5 hypothesis) is
**project-conditioned** data. Generic synthetic function-calling in excess
reintroduces v4's cross-project hallucination. So generation is **not** generic:

1. **Project-conditioned scenarios only.** Inlet B samples a real project from a
   catalog (`fire-map.wfca.com`, `agentMemory`, `anvil`, `psde_mz_test`,
   `mz-personal-archived` — the same set the v5 pilot kept) and draws argument
   values (paths, filenames, symbols) from **real value pools mined from that
   project's own `mem_tool_calls` rows**, then normalizes to `<TRUSTED_ROOT>`.
   A generated `Read` on the `agentMemory` project references a path that
   actually exists under `agentMemory`, not an invented one. This is APIGen's
   "executable against the real signature", specialized to "resolvable against
   the real project".
2. **Blend policy (the dilution guard).** The training mix is
   **in-house project-conditioned CORE + a capped curated/generated SLICE**:
   - CORE = the v5-pilot 5k project-conditioned real rows (unchanged,
     authoritative — HANDOFF "locked-in").
   - SLICE = curated real methodology traces (Part 2) + generated
     project-conditioned rows (Inlet B) + a *small* curated general slice
     (Hermes/xLAM, per the shortlist), **each capped**.
   - Hard caps: `generated_pct ≤ 0.20`, `general_public_pct ≤ 0.15`,
     `per_methodology_cap_pct ≤ 0.15`, `max_synthetic_pct ≤ 0.15` (Part 1 cap
     stays). Synthetic ∪ generated ∪ public **combined ≤ ~35%** of the mix so
     the model still primarily learns from real project traces.
3. **Ablation step (mandatory, not optional).** Before any blend ships, run the
   ablation: train (or LoRA-probe) at ≥3 blend ratios (e.g. CORE-only,
   CORE+10% generated, CORE+20% generated) and compare on the BFCL-style eval.
   Ship the ratio that **lifts the weak categories without regressing
   `cross_project` / `ood_project`** (the v4 failure signature). If more generic
   data regresses cross-project — the predicted dilution — the ablation catches
   it *before* production, with evidence. This is the concrete guard the
   shortlist doc's step 5 ("ablate the blend ratio") asks for.

## Verifiable-generation stages, concretely (what "executes against the signature" means here)

We do not have a sandbox that *runs* arbitrary tools, and we must not (DB is
read-only; no side effects). "Executes against the real signature" is therefore
realized as **static + resolvable validation against the tool's actual JSON
Schema and the real project filesystem/DB** — which is exactly what a
function-calling dataset needs, since the training target is the *call*, not the
tool's runtime effect.

- **Stage 1 — FORMAT.** Reuse `validate_tool_calls.parse_tool_calls()`
  verbatim: `<tool_call>` blocks parse, each has `name` + `arguments`, JSON is
  well-formed. Reject on any failure. (Already implemented — no new code.)
- **Stage 2 — SIGNATURE (the APIGen "executes" analog).** The tool's real
  parameter schema is the signature. `validate_tool_calls.py` **already** does
  this for the 5 canonical tools via its embedded `jsonschema.Draft7Validator`
  block (lines ~268–283) loaded from
  `data/processed/qwen25_tools/<v>/tool_schemas.json`. **Extend** it to:
  (a) load the *full* in-house tool registry (Claude tool defs + Anvil MCP tool
  defs + agent-memory tool defs) instead of the 5-tool default, and
  (b) expose a reusable `validate_call_against_registry(call, registry) →
  (ok, reason)` function the factory imports (today the schema loop is inline in
  `parse_tool_calls`). Unknown tool name ⇒ reject. Required-arg missing ⇒
  reject. Wrong type / failed enum ⇒ reject. This is the single most important
  reuse-and-extend in the plan.
- **Stage 3 — SEMANTIC / ANSWER-VERIFIABLE.** APIGen's third stage asks "is the
  answer actually correct?". Our answer-verifiability, per row type:
  - **path-bearing calls** (`Read`/`Edit`/`Write`/`Grep` file_path): the path
    must resolve under the row's project root (or be a plausible new file for
    `Write`). Reuse `audit_dataset.py`'s deterministic `Path(p).exists()` /
    `_path_exists` machinery and its fabricated-path gate.
  - **no fabrication**: no invented git hashes / PR numbers — reuse
    `audit_dataset.py` `GIT_HASH_RE` + `git cat-file -e` check.
  - **abstain rows** (irrelevance): the *correct* answer is **no tool_call** —
    verify the assistant turn is text-only asking for clarification, matching
    the pattern `synth_abstain_rows.py` already produces. Reuse that generator
    as the abstain inlet.
  - **sequence-level** (multi-turn curated traces): verify-after-mutate proxy
    holds (a read-back follows a write in the same session), and no
    identical-call loop — reuse `validate_tool_calls.AntiLoopDetector`.
  A row must pass **all three stages** to be written. The gate is fail-closed:
  when a stage cannot be evaluated (e.g. tool has no registered schema), the row
  is **rejected**, not admitted.

## BFCL-style eval harness (extend `eval_harder.py`)

`eval_harder.py` already provides: llama-server lifecycle, OpenAI-compat `_chat`,
deterministic `score_scenario`, per-category fixtures + scoreboard.md, a
configurable gate. We add **BFCL-aligned categories** as new fixture files and a
richer scorer; we do **not** rewrite the harness.

| Category | What it tests | New/retained | Scoring (deterministic) |
|---|---|---|---|
| `simple` | one clear tool, one correct call | NEW | correct tool name + required args present + args resolve |
| `parallel` | one prompt needs ≥2 calls at once | NEW | all expected calls emitted, none extra |
| `multiple_choice` | several tools available, exactly one right | NEW | the right tool chosen, wrong tools absent |
| `irrelevance_abstain` | prompt needs NO tool (or missing info) | NEW | **no** tool_call fired; text asks for clarification (reuse abstain logic) |
| `path_bias` | model must not rewrite user-typed paths | RETAINED | existing `forbidden_substrings`/`expected_path_in_args` |
| `cross_project` | name exists in N projects, pick right one | RETAINED | existing |
| `ood_project` | project not in training — don't fabricate | RETAINED | existing |
| `fabrication` | vague prompt — don't invent PRs/branches | RETAINED | existing |

Output: a **per-category before/after scorecard** (`eval_harder.py` already
writes `scoreboard.md`). Phase 1 runs it on the current production v4/v5 model to
establish the **baseline number** *before any factory work* — so every later
phase can show real deltas, not assertions. The `irrelevance_abstain` scorer is
the one genuinely new scoring branch (assert absence of tool_call); the rest
reuse the existing `score_scenario` shape.

## Reuse-and-extend map (what the factory imports, never reinvents)

| Capability | Existing home | Factory action |
|---|---|---|
| DB read (CSV via psql_wrapper) | `build_v5_pilot_dataset.query_source_rows` | import |
| Secret redaction | `app.redact` / v5 fallback | import (always on) |
| `<TRUSTED_ROOT>` path normalize | `v5_schema.normalize_*` | import |
| `[Project]` block + Qwen3 render | `v5_schema.render_row` | import (single output shape) |
| Format parse (Gate S1) | `validate_tool_calls.parse_tool_calls` | import |
| Schema validate (Gate S2) | `validate_tool_calls` jsonschema block | **extend** → `validate_call_against_registry` + full tool registry |
| Fabricated-path/hash checks (Gate S3) | `audit_dataset.py` `_path_exists`, `GIT_HASH_RE` | import |
| Anti-loop / verify-after-mutate | `validate_tool_calls.AntiLoopDetector` | import |
| Abstain rows + abstain scoring | `synth_abstain_rows.py` | import as Inlet-B abstain generator |
| Curation filters (empty-args, repetition, per-tool cap, session split) | `build_v3_dataset.py` | import |
| Eval harness (server, scoring, scoreboard) | `eval_harder.py` | **extend** with BFCL categories |
| Post-build data-quality gate | `audit_dataset.py` | run unchanged on factory output |

New code is deliberately thin: an **inlet-B generator** (project-conditioned
sampler), the **gate orchestrator** (chains S1→S2→S3 over both inlets), the
**registry loader** (assemble the full in-house tool-schema set once), and
**four eval fixture files** + one abstain scorer branch. Everything else is a
call into an existing, already-verified helper.

## Phased rollout (each data-writing phase: snapshot + rollback + human review)

Per the Definition-of-Done and Incident contracts (CLAUDE.md): every phase that
writes data is destructive-by-default and gated on a snapshot, an explicit
rollback, and a separate-context audit before it is called done.

### Phase 1 — Eval harness + baseline audit (READ-ONLY, no writes)
- Add the 4 BFCL fixture files + abstain scorer branch to `eval_harder.py`.
- Run the harness on the current production model → `scoreboard.md` baseline.
- Run `audit_dataset.py` (existing) over the current v5-pilot dataset → baseline
  data-quality number.
- **Writes nothing to the DB or training set.** Produces baseline artifacts only.
- **AC1.1** scoreboard.md exists with a per-category rate for the current model,
  all 8 categories present. **Evidence:** paste the scoreboard table.
- **AC1.2** baseline `audit_dataset.py` PASS/FAIL recorded for v5-pilot.
  **Evidence:** paste gate table.
- **AC1.3** OQ-1 (severity values) resolved with the user before Phase 3.
- **Rollback:** none needed (read-only). **Risk:** low.

### Phase 2 — The shared validation gate (build + unit-test in isolation)
- Extend `validate_tool_calls.py` with `validate_call_against_registry` + full
  registry loader. Build the gate orchestrator (S1→S2→S3) as an importable
  module with **unit tests only** — no dataset produced.
- Assemble the in-house tool registry (Claude + Anvil + agent-memory schemas);
  count how many of the 97 distinct tool_names have a registered schema (tools
  without one are gate-rejectable — that coverage number is a Phase-2 output).
- **AC2.1** gate unit tests: a known-good call PASSes all 3 stages; a
  bad-name / missing-arg / wrong-type / fabricated-path / abstain-with-tool-call
  each REJECTs at the correct stage. **Evidence:** pytest output.
- **AC2.2** registry coverage report: N/97 tool_names have schemas; the top-7
  tools (Bash/Read/Edit/Grep/Write/Glob + one MCP) are all covered.
- **Writes no training data.** **Rollback:** delete the module (no persisted
  state). **Risk:** low-medium (registry assembly is the unknown).

### Phase 3 — CURATE inlet (real traces through the gate) — FIRST DATA WRITE
- Wire Inlet A: pull gate-pass rows (the 28,658 pool), run Part 2 filters, run
  every kept row/sequence through the Phase-2 gate, render via `v5_schema`.
- **Snapshot before write:** the exact source query + row-id manifest are
  recorded; output goes to a **new** versioned dir
  (`data/processed/qwen3_tools/factory-vX/`), never overwriting v5-pilot.
- Run `audit_dataset.py` + the mask gate on the output.
- **Separate-context auditor** (aa_auditor) reviews against these ACs before done.
- **AC3.1** every written row passed all 3 gate stages (gate_stages_passed=3 in
  provenance). **AC3.2** 0 unredacted secrets, 0 fabricated paths, 0 mis-linked
  prompts (audit_dataset PASS). **AC3.3** AUDIT.md funnel + per-methodology-tag
  + per-project counts; caps respected. **AC3.4** mask gate green.
- **Rollback:** delete the versioned dir; v5-pilot is untouched. **Risk:** medium.

### Phase 4 — GENERATE inlet (project-conditioned synthesis through the gate)
- Wire Inlet B: project catalog + real value-pool sampler → candidate calls →
  the *same* Phase-2 gate → render. Abstain rows via `synth_abstain_rows.py`.
- Same snapshot/new-dir/audit/auditor discipline as Phase 3.
- **AC4.1** generated rows are project-conditioned (every path resolves under its
  row's project root; 0 cross-project path leaks). **AC4.2** `synthetic=true` +
  `source=generated` tagged; combined synthetic∪generated∪public ≤ 35% cap.
  **AC4.3** all 3 gate stages passed; audit + mask gate green. **AC4.4** first
  build: every generated row hand-sampled (≥100) for realism.
- **Rollback:** delete the versioned dir. **Risk:** medium-high (fabrication
  risk — mitigated by the gate + hand-sample).

### Phase 5 — Blend + ablation + eval (the go/no-go)
- Compose CORE + capped SLICE at ≥3 ratios; for each, LoRA-probe/train and run
  the Phase-1 eval harness. Compare per-category deltas vs baseline.
- **AC5.1** ablation table: ≥3 ratios × 8 categories, before/after. **AC5.2** the
  chosen ratio lifts ≥1 weak category **without** regressing `cross_project` /
  `ood_project` (evidence: scoreboard deltas). **AC5.3** if no ratio clears
  AC5.2, ship **nothing** and record the negative result — CORE-only stays.
- **Rollback:** training artifacts are recovery points (memory
  `feedback_keep_working_artifacts`); no production swap until AC5.2 passes with
  a separate-context audit. **Risk:** high (this is the real experiment).

## Risks (factory-level)

1. **Generation dilution reintroduces v4 hallucination.** *Mitigation:* hard
   caps + the mandatory Phase-5 ablation gated on no `cross_project` regression.
2. **Gate false-confidence.** Static+resolvable validation is not runtime
   execution; a call can pass the schema and still be semantically wrong.
   *Mitigation:* Stage-3 answer-verifiability + first-build hand-sampling; never
   claim "executed" — claim "schema-valid + project-resolvable".
3. **Registry incompleteness.** Tools without a registered schema are
   gate-rejected, which could starve the generated set of long-tail tools.
   *Mitigation:* Phase-2 coverage report; expand registry before Phase 4 if
   top-N coverage is thin.
4. **Severity-value bug (OQ-1) silently produces empty lesson set.**
   *Mitigation:* fixed in Phase 1 AC1.3 before any lesson-conditioned build.
5. **Two output splits (SFT vs DPO) mishandled.** Preference rows must not leak
   into the SFT file (Part 1). *Mitigation:* separate files enforced at the
   renderer; audit counts both.

## Rollback (factory-level)

- All factory output writes to **new versioned dirs**; v4 production GGUF and the
  v5-pilot dataset are never overwritten (HANDOFF: v4 authoritative, v5-pilot
  locked-in).
- Each phase records its exact source query + row-id manifest, so any build is
  reproducible or discardable by deleting one directory.
- No production model swap occurs before Phase 5 AC5.2 passes a separate-context
  audit. The negative result (ship nothing) is an explicitly valid outcome.

## Open questions for the user (OQ)

- **OQ-1 (blocking).** `mem_lessons.severity` is `critical`/`warning`/`info`, not
  `high`/`critical`. Confirm the methodology-lesson target set: `critical`-only
  (31 active) or `critical`+`warning` (50 active)? The earlier Part 1 config is
  wrong as written and will select 0 rows for "high".
- **OQ-2.** Registry scope: which tool-schema sources are authoritative for
  Stage 2 — Claude tool defs, Anvil MCP defs, agent-memory MCP defs, or all
  three unioned? (Affects which of the 97 tool_names are validatable vs
  gate-rejected.)
- **OQ-3.** Public slice: do we actually pull a Hermes/xLAM curated slice now
  (needs the gated xLAM access or minpeter's mirror — shortlist doc), or defer
  the public inlet and prove the generate+curate inlets first? Recommend defer.
- **OQ-4.** Generation volume target for the first build (how many synthetic
  project-conditioned rows), given the ≤20% generated cap against a 5k CORE
  (⇒ ~1k generated ceiling for a 6k blend). Confirm CORE size for the factory
  blend (stay at v5-pilot 5k, or grow the real CORE first).
- **OQ-5.** SFT-only for the first factory build, or include the DPO preference
  split (Part 1a) now? Recommend SFT-only first; DPO as a later pass on a sane
  SFT policy (matches Part 1's two-split rationale).
- **OQ-6.** Do we need an execution sandbox later (true APIGen "executes") for a
  subset of pure-function tools, or is schema+resolvable validation sufficient
  for the tool-call training target? Recommend: sufficient for now; revisit only
  if eval shows argument-correctness (not tool-selection) is the failing axis.
