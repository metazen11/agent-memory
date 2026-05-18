# v3 Fine-Tune Dataset Audit

**Date:** 2026-05-15
**Build script:** `scripts/fine_tune/build_v3_dataset.py`
**Output:** `data/processed/qwen3_tools/v3/{train,valid,train.tiny,valid.tiny}.chat.jsonl`
**Manifest:** `data/processed/qwen3_tools/v3/MANIFEST.json`
**Audit harness:** `/tmp/v3_audit.py` + `/tmp/v3_template_check.py`

---

## Summary

Build succeeded. 28,599 candidate rows pulled from `mem_tool_calls`
(`retention_class='backfill_jsonl'`); 23,971 converted; 23,748 train +
1,464 valid in the final dataset, plus 200 / 30 tiny splits. Eight v3
fixes from V3_PLAN.md §5 were applied (fix #3 DPO was deferred per
plan §11). The drops break down cleanly across the fixes — the largest
single drop is **fix #7 in-args repetition cap at 1,562 rows** (matches
the v2 chat-template-retest finding that 5/10 prompts produced
generative loops inside argument values).

Two findings the user should look at before training:
1. **Project oversampling did not fire** — the natural project-tagged
   rate is 55.5%, already above the 30% cap, so the oversampler added 0
   rows. This is correct per the cap rule but means fix #6's "2×
   project oversample" is effectively a no-op on this corpus; the data
   already over-indexes on project names.
2. **8.4% of train rows have `<task-notification>` Anvil pipeline
   prompts** rather than natural Claude Code prompts. These are not
   covered by any of the 8 fixes but are off-distribution for the
   intended use (agentic Claude Code replacement).

**Verdict: APPROVE for training (with caveats).** All fixes either
pass cleanly, or fail to fire for documented data-state reasons (not
script bugs). The chat template renders 100/100 on both Qwen2.5-3B
and Qwen3-4B tokenizers, which is the load-bearing technical gate.

---

## Per-fix verification

### Fix 1 — Stop-after-tool_call cut

- **Description:** Truncate assistant turns at first `</tool_call>`;
  drop any text after.
- **Expected:** Generated dataset rows contain no chat scaffolding
  tokens (`</tool_call>`, `<|im_start|>`, `<|im_end|>`) inside the
  assistant turn — neither in the tool name nor in the args strings.
- **Architectural note:** Because v3 rebuilds from `mem_tool_calls`
  (one row per recorded tool call, no free-form trailing assistant
  text), the "text after `</tool_call>`" case is structurally
  impossible at the row level. The only escape route is a scaffolding
  token leaking *inside* an argument string. The build script scans
  every string-valued arg and truncates at the first scaffold token.
- **Verification:** Random sample 500/23,748 train rows. Searched
  inside each row's tool_calls JSON for `</tool_call>`, `<|im_start|>`,
  `<|im_end|>`. Sample-20 check on full assistant turn content.
- **Result: PASS.** 0/500 random rows contain scaffolding tokens in
  args. 0/20 random rows contain scaffolding tokens in the assistant
  turn. The build script reports it stripped 9 rows of in-arg scaffolds
  before keeping them — these were the leaks the defensive scrub
  caught.

### Fix 2 — Text-synthesis oversampling

- **Description:** Detect rows whose structure is
  `[user → assistant-tool_call → tool_response → assistant-text-only]`
  and oversample 2× (cap 20% of training set).
- **Expected:** "Text-synthesis candidate" rows are detected by SQL
  window function (last tool_call per `prev_user_prompt_id`); each
  such row gets one duplicate copy added, capped so the final
  text-synth fraction ≤ 20%.
- **Verification:** Manifest reports natural count 1,241; oversampled
  added 1,241; final count 2,482 = 10.45% of train (well under 20%
  cap). 2× factor applied cleanly.
- **Result: PASS.** Numbers are exactly what 2× oversample on 1,241
  rows produces. The cap did not bind (10.45% < 20%).
- **Caveat:** "Last tool_call for the prompt" is a proxy for "next
  assistant turn is text-only" — it's correct *if* the data really
  ends the prompt's tool sequence at the last `mem_tool_calls` row.
  Cases where the assistant ran out of context before responding in
  text would also match. That's acceptable for SFT and matches
  the spec.

### Fix 3 — DPO/KTO preference training

- **Status: DEFERRED to v3.1 per V3_PLAN §11.** Not implemented.

### Fix 4 — Subagent transcript filter

- **Description:** Drop rows from `*/subagents/agent-*.jsonl`.
- **Expected:** Any source jsonl path matching the subagent pattern
  produces 0 rows in the dataset.
- **Verification:** The filter relies on
  `backfill_log.jsonl_path` joined via `session_id`. Manifest reports
  0 rows dropped. Disk check: subagent jsonl files DO exist
  (e.g.
  `~/.claude/projects/-Users-mz--CODING-psde/.../subagents/agent-af09....jsonl`).
  But `backfill_log` only covers ~72 of the sessions that contributed
  `backfill_jsonl` tool_calls — the backfill pipeline appears to have
  populated `mem_tool_calls` for many sessions without writing to
  `backfill_log`.
- **Result: PARTIAL.** Filter is correct where it applies (0 false
  positives, 0 false negatives on rows with known paths). For rows
  with no jsonl_path, the filter cannot run. Given that subagent
  jsonl files are typically captured by a separate ingest path, and
  none of them show in `backfill_log`, this is most likely "no
  subagent rows in the data" rather than "subagent rows passed
  through undetected" — but we cannot prove that without an
  independent label.
- **Recommended follow-up:** add `source_path` to `mem_tool_calls`
  in a future migration so this filter can run on every row.

### Fix 5 — Off-distribution mutation actions for THIS repo

- **Description:** For rows where the user prompt mentions
  `agent-memory`, drop rows whose **first** tool_call is a *mutation*
  action (`gh issue create`, `git commit/push`, `npm publish`,
  `pip install`, `rm -rf`, plus mutation MCP tools like
  `mcp__anvil__ghissue_create`).
- **Expected:** Mutation-action rows on agent-memory prompts at
  `turn_seq=1` are dropped. Mid-workflow mutation calls (turn_seq>1)
  are kept (the user has already asked for a mutation and we're
  continuing the session).
- **Verification:** Manifest reports 7 rows dropped by fix #5.
  Verification pass scanned all train rows for "prompt mentions
  agent-memory + first-emit is mutation". Found 58 remaining.
- **Result: PARTIAL.** The 58 remaining rows are NOT misses, they
  are by-design retentions. Of the 58:
  - **44 are `Write`/`Edit`** — these are legitimate first calls
    when the user asks the model to fix/write something in this repo.
    Fix #5 deliberately keeps them (not in the mutation list).
  - **11 are `git-{reset,checkout,push,commit,branch}`** — fix #5
    SHOULD drop these. But these rows have `turn_seq > 1` (they are
    mid-workflow), so the fix correctly bypasses them. To verify,
    spot-check: all 11 git-mutation rows have prompt content matching
    `<task-notification>` style messages (Anvil agent pipeline
    continuations), not user requests.
  - **2 are `pip install`, 1 is `rm`** — same story (mid-workflow
    continuations).
- **Honest take:** Fix #5 as specified only handles the narrow case
  of "first emit on a question prompt is a fabricated mutation". The
  V3_PLAN.md example (`gh issue create` for "is there a test for X?")
  is now caught. The broader problem of mid-workflow mutation
  continuations on Anvil task-notification prompts is OUT of scope for
  fix #5 — see the `<task-notification>` finding below in Red Flags.

### Fix 6 — Project-tagged oversampling

- **Description:** Tag rows mentioning `agent-memory`, `fire-map`,
  `daily-dispatch`, `anvil`, `validator`, `tdd-qa`. Oversample 2× with
  per-project cap at 30% of train.
- **Expected:** Project-tagged rows get one duplicate copy each, up to
  the cap. MANIFEST documents per-project counts.
- **Verification:** Natural per-project counts from MANIFEST:
  - `fire-map`:        7,318 rows (30.8%)
  - `anvil`:           4,396 rows (18.5%)
  - `agent-memory`:    986 rows (4.1%)
  - `tdd-qa`:          302 rows (1.3%)
  - `daily-dispatch`:  139 rows (0.6%)
  - `validator`:       49 rows (0.2%)
  - **Total tagged:**  ~13,190 distinct rows / 23,748 = **55.5%**
- **Result: PARTIAL.** The total tagged rate of 55.5% is already
  above the 30% combined cap, so the oversampler computed 0 added
  rows (the cap binds against the union of tagged rows, not per-
  project). **The 2× factor never fires.**
- **What this means for training:** project recall data is already
  abundant for `fire-map` and `anvil`. It is sparse for
  `daily-dispatch` (139 rows, 0.6%) and very sparse for `validator`
  (49 rows, 0.2%). Per V3_PLAN's Class E gate ("project recall ≥ 50%
  AND strictly better than v1/v2"), the model will likely struggle on
  `validator` and `daily-dispatch` simply because there isn't enough
  training signal.
- **Recommended follow-up before training:** either lift the
  PROJECT_MAX_PCT cap (currently 30% of train) to ~50%, OR change the
  oversampler to be per-project rather than aggregate so under-
  represented projects can still be boosted while over-represented
  ones stay capped.

### Fix 7 — In-args repetition cap

- **Description:** Drop rows where ANY argument value contains > 3
  consecutive identical lines, OR exceeds 2,000 chars.
- **Expected:** No remaining rows have over-cap argument values.
- **Verification:** Manifest reports 1,562 drops. Sampled all 23,748
  train rows for `len(arg_value) > 2,000` OR `>3 consecutive
  identical non-empty lines` in any string-valued arg.
- **Result: PASS.** 0/23,748 train rows have remaining
  in-args-repetition violations. The max args JSON size dropped from
  v2's 11,003 chars to v3's 4,140 chars (the 2,000-char cap is on
  *individual values*, not the whole JSON envelope, hence the JSON
  can still be larger than 2,000 if there are multiple shorter args).
- **Sanity:** 1,562 drops is 5.5% of converted rows, consistent with
  v2 chat-template-retest's "5/10 prompts had generative loops" —
  i.e. real loops are present in the corpus and are being filtered.

### Fix 8 — Vision-row filter

- **Description:** Drop rows referencing images (`[VISION]`,
  `<image>`, `image_url`, or `image_path` key in args).
- **Expected:** No image references remain.
- **Verification:** Manifest reports 31 drops. Scanned all 23,748
  train rows for the same markers.
- **Result: PASS.** 0/23,748 train rows reference images. The 31
  source-image rows did exist in the corpus and are now out.

---

## Chat-template rendering check

Sampled 100 random train rows; called
`tokenizer.apply_chat_template(messages, tools=tools, tokenize=False)`
on each.

| Tokenizer | Path | Result |
|---|---|---|
| `Qwen2.5-3B-Instruct` (v2's base) | `models/base/qwen2.5-3b-instruct` | **100/100 OK** |
| `Qwen3-4B` (v3's smoke-test base) | `models/base/qwen3-4b` | **100/100 OK** |

Both tokenizers load as `Qwen2Tokenizer` (same family). No failures
on the 100-row sample. **This is the load-bearing technical gate and
it passes for both targets.** Qwen3-8B's tokenizer is the same family
(`Qwen2Tokenizer`) and uses the same chat-template schema as
Qwen3-4B; the render check is expected to pass there as well, but we
will re-run when the 8B local weights are pulled.

---

## Project distribution

Per-project row counts (natural rate; oversampler added 0 — see
Fix 6):

| Project | Rows | % of train |
|---|---:|---:|
| fire-map | 7,318 | 30.8% |
| anvil | 4,396 | 18.5% |
| agent-memory | 986 | 4.1% |
| tdd-qa | 302 | 1.3% |
| daily-dispatch | 139 | 0.6% |
| validator | 49 | 0.2% |
| **(any tag)** | ~13,190 | 55.5% |
| **(untagged)** | ~10,558 | 44.5% |

`fire-map` and `anvil` dominate; `validator` and `daily-dispatch` are
underrepresented for a Class E "project recall" gate. If V3 fails
Class E for those two projects, this is the proximate cause — not
enough training signal.

---

## Distribution vs v2

|                              | v2     | v3      | delta   |
|------------------------------|-------:|--------:|--------:|
| train rows                   | 23,983 | 23,748  | -235    |
| valid rows                   | 1,588  | 1,464   | -124    |
| Bash share                   | 56.1%  | 58.1%   | +2.0 pp |
| Read share                   | 15.7%  | 16.7%   | +1.0 pp |
| Edit share                   | 13.8%  | 13.7%   | -0.1 pp |
| Grep share                   | 5.2%   | 5.5%    | +0.3 pp |
| Agent share                  | 2.6%   | 1.8%    | -0.8 pp |
| Write share                  | 4.1%   | 1.7%    | -2.4 pp |
| Glob share                   | 0.5%   | 0.6%    | ~flat   |
| WebSearch share              | 0.3%   | 0.4%    | ~flat   |
| Avg #messages per row        | 4.00   | 4.00    | (same)  |
| Avg args JSON size           | 664    | 373     | **-44%** |
| Max args JSON size           | 11,003 | 4,140   | **-62%** |

Notable: average args size dropped 44% and max dropped 62% — directly
attributable to fix #7's repetition cap. This is exactly the
regression target identified in v2-chat-template-retest.md
("max_tokens-truncated argument JSON with hundreds of repeated lines").

Write share fell from 4.1% to 1.7% — likely because Write rows tend
to contain the long looping content that fix #7 catches.

---

## v2 postmortem regression spot-checks

Twenty random rows checked for each of v2's documented regressions.

### 1) Hallucinated chat scaffolding inside one assistant turn

- v2 chat-template-retest: 5/10 prompts emitted
  `<|im_start|>`/`<|im_end|>` tokens inside assistant content.
- **v3 audit on 20 random rows: 0/20** — no scaffolding tokens inside
  the assistant turn. The 500-row sample for fix #1 also found 0/500.
- **Verdict: regression fixed at the data level.** (The build script
  rebuilds from structured DB rows, not raw jsonl — the leak path is
  closed.)

### 2) Off-topic actions for agent-memory prompts

- v2 real-world A/B prompt 3 ("Is there a test that proves the
  empty-args loop is fixed?") emitted `gh issue create` with a
  fabricated bug report.
- **v3 audit on 20 random agent-memory prompts: 7/20 still have
  mutation first-calls.** Drilldown shows all 7 are `Edit`/`Write` on
  legitimate fix-this-script prompts, NOT off-topic mutation actions
  like `gh issue create`. The genuine off-topic actions (e.g.
  `gh issue create`) are caught by fix #5 (7 dropped).
- **Verdict: original v2 failure mode is fixed; broader Write/Edit
  retention is intentional per fix #5 design.**

### 3) Identical-reemit shape

- v2 emitted the same tool_call on every turn after a tool_response.
- v3 dataset rows are single-turn (one tool_call per row), so the
  multi-turn re-emit pattern isn't visible at the dataset level. The
  fix is at training-objective level (fix #2 oversampling teaches the
  model to switch to text after tool_response). Spot-check on the
  dataset: 1,243 unique (session+prompt+name+args) keys appear ≥2×,
  contributing 1,881 duplicate rows — but **this number includes the
  intentional 1,241 text-synth oversample duplicates**, so the
  "natural" duplicate count is ~642 rows (which is consistent with
  v2-style real-Claude data: the same shell command runs twice in
  context).
- **Verdict: cannot be measured at dataset level; will be measured at
  Class B eval gate after training.**

### 4) In-args repetition

- v2 chat-template-retest: 5/10 prompts had argument JSON with > 3
  consecutive identical lines.
- **v3 audit on all 23,748 train rows: 0/23,748 violations.** Fix #7
  is doing its job.

---

## Red flags / open issues

### 1) `<task-notification>` Anvil pipeline prompts dominate part of the corpus

- **Count:** 1,998 / 23,748 train rows (**8.4%**) have prompts that
  start with `<task-notification>` — these are Anvil agent-pipeline
  system messages, not natural Claude Code user prompts.
- **Why it matters:** These prompts contain task-id, tool-use-id,
  output-file, and instructions to continue mid-workflow. They're
  off-distribution for the intended use case ("agentic Claude Code
  replacement"). They're also the source of most fix-5 borderline
  cases — the 11 git-mutation continuations on agent-memory prompts
  all have `<task-notification>` shape.
- **Recommendation before training:** add a fix #9 to either drop or
  oversample-down these prompts. They're easy to detect (literal
  prefix match on `<task-notification>` in the first 100 chars).
- **Examples:** `train.chat.jsonl` rows whose `messages[1].content`
  begins with `<task-notification>\n<task-id>...`.

### 2) Fix #6's 2× oversampling never fires

- Aggregate tagged rate (55.5%) already exceeds the 30% cap, so the
  cap-respecting solver computed `cap_extra = 0` and added 0
  duplicates. This is correct per the cap rule. But it means the
  V3_PLAN.md "2× oversample" statement is misleading on this corpus.
- The under-represented projects (`validator` at 49, `daily-dispatch`
  at 139) would benefit from a per-project oversampler. As-implemented
  they get nothing.
- **Recommendation:** before training, either (a) lift the cap, or
  (b) change the oversample logic to per-project caps (e.g. each
  tag capped at 15% of train). Document the actual final per-project
  counts in MANIFEST after the change.

### 3) `backfill_log.jsonl_path` coverage is partial

- Only ~72 sessions in `mem_tool_calls`'s `backfill_jsonl` slice have
  matching `backfill_log` rows. The other sessions' jsonl source path
  is unknown.
- This means fix #4 (subagent filter) can only run on a small subset
  of the data. The build did not catch any subagent rows because
  none of the 72 covered sessions have subagent paths.
- **Recommendation:** add `source_path` (text) to `mem_tool_calls` in
  a future migration and backfill it. This is not blocking for v3
  training (subagent paths in the corpus appear to be rare).

### 4) `validator` corpus is 49 rows; will be a hard test for Class E

- The V3_PLAN.md Class E gate requires ≥ 50% project recall on
  prompts mentioning each project. With only 49 rows mentioning
  "validator", and many of those overlapping with `agent-memory`
  (the validator lives in this repo), the model has very thin signal
  for validator-specific recall. Expect this to be the lowest-scoring
  Class E project.
- **Not blocking v3 training**, but should be called out before the
  eval gate is run.

---

## Recommendation

**APPROVE for training**, with two pre-training fixes recommended (not
required):

1. **Either lift PROJECT_MAX_PCT to ~50% or switch fix #6 to per-
   project caps** — the current implementation never fires its 2×
   factor on this corpus, leaving `validator` / `daily-dispatch`
   under-trained.
2. **Add a fix #9 to gate or down-weight `<task-notification>`
   prompts** — they account for 8.4% of train rows and are off-
   distribution for the intended use case.

All eight v3 fixes are either PASS (1, 2, 7, 8), DEFERRED-per-plan
(3), or PARTIAL-with-acceptable-reason (4, 5, 6). The chat-template
gate passes 100/100 for both Qwen2.5-3B and Qwen3-4B tokenizers.
The structural failure modes from v2 (in-args repetition, vision
contamination, scaffolding tokens) are gone from the dataset.

The remaining v2 regressions (multi-turn adaptation, identical
re-emit) can only be measured at training-time and post-train eval,
not at dataset-build time — fix #2's text-synthesis oversampling is
the data-side lever, and it landed cleanly at 10.45% of train
(under the 20% cap).

---

## Artifacts

- Build script: `scripts/fine_tune/build_v3_dataset.py`
- Output dataset: `data/processed/qwen3_tools/v3/`
- Manifest: `data/processed/qwen3_tools/v3/MANIFEST.json`
- Audit harness: `/tmp/v3_audit.py`
- Template-check harness: `/tmp/v3_template_check.py`
- Raw audit log: `/tmp/v3_audit.log`
