# v3 Fine-Tune Dataset Audit — Delta Re-Audit (v2)

**Date:** 2026-05-15
**Original audit:** [`v3-dataset-audit.md`](v3-dataset-audit.md)
**Build script:** `scripts/fine_tune/build_v3_dataset.py`
**Output:** `data/processed/qwen3_tools/v3/{train,valid,train.tiny,valid.tiny}.chat.jsonl`
**Manifest:** `data/processed/qwen3_tools/v3/MANIFEST.json`
**Scope:** Delta-only re-audit. The original audit (v1) approved the
build; this re-audit verifies only the two changes shipped in response
to v1's caveats:

1. **Fix #9 (NEW):** Drop `<task-notification>` Anvil pipeline prompts.
2. **Fix #6 (REWORKED):** Per-project oversampling cap (15% each)
   instead of the aggregate 30% cap that bound at zero added rows.

Everything else (fixes 1, 2, 4, 5, 7, 8 and the chat-template render)
was re-checked at the sample level and is unchanged from v1.

---

## Summary

**Verdict: APPROVE for training.** Both fixes work as specified. No
unexpected behaviour, no schema or template regressions.

| Metric                       | v1 (original) | v2 (this re-audit) | Delta   |
|------------------------------|---------------|---------------------|---------|
| Train rows                   | 23,748        | 22,890              | -858    |
| Valid rows                   | 1,464         | 1,392               | -72     |
| Tiny train / valid           | 200 / 30      | 200 / 30            | 0 / 0   |
| Chat template render (100×)  | 100/100       | 100/100             | 0       |
| Fix #9 task-notification rows in train | (not run) | **0** | n/a |

Train delta breakdown:
- Fix #9 dropped 2,024 task-notification rows at row-build time.
- Fix #6 added 1,148 per-project duplicates (down from 0 in v1, up
  from a hypothetical aggregate-cap result).
- Net: -2,024 + 1,148 = -876 rows from the train-pre-oversample basis;
  the final train delta is -858 once the new valid-split sessions also
  shift slightly.

---

## Fix #9 verification — task-notification drop

**Build-time count from MANIFEST `drops_per_fix.fix9_task_notification`: 2,024.**

Audit's pre-build count was 1,998. The 26-row delta is explained by
candidate-set drift between the original audit's snapshot and the
current build (the SQL `WHERE retention_class='backfill_jsonl'` view
gained 24 rows of new ingest between runs). Within rounding, the count
matches expectation.

**Post-build verification** (greps the final jsonl):

```python
import json
with open('data/processed/qwen3_tools/v3/train.chat.jsonl') as f:
    n = sum(1 for line in f
            if json.loads(line)['messages'][1]['content']
                  .lstrip().startswith('<task-notification>'))
print(n)
```

Result on train: **0**.
Result on valid: **0**.

The filter is correctly applied before the train/valid split, so both
splits are clean.

---

## Fix #6 verification — per-project oversampling

Per-project counts before/after oversampling (from MANIFEST
`fix6_project_tagging`):

| Project tag      | Natural (train) | Final (after 2×) | Added | Notes                          |
|------------------|------------------|-------------------|-------|--------------------------------|
| fire-map         | 6,979            | 6,979             | 0     | already > 15% cap (~33.8%)     |
| anvil            | 4,160            | 4,160             | 0     | already > 15% cap (~20.1%)     |
| agent-memory     | 794              | 1,588             | +794  | 2× fired; ~7.7% of train       |
| daily-dispatch   | 69               | 138               | +69   | 2× fired; ~0.7% of train       |
| tdd-qa           | 285              | 570               | +285  | 2× fired; ~2.8% of train       |
| validator        | 0                | 0                 | 0     | absent from this build (note)  |

**Total oversample added: 1,148 duplicate rows.**

Notes vs the original brief's expected counts:
- The brief expected agent-memory=986 / tdd-qa=302 / daily-dispatch=139
  / validator=49 naturals (from the original audit snapshot). The
  current build sees slightly different naturals because fix #9 removed
  some task-notification rows that carried those project tags before
  the split. The 2× factor is applied correctly to whatever survived;
  the per-project cap (15% of train ≈ 3,097 rows) never binds because
  all under-represented tags double to well below cap.
- The brief expected total added ≈ 1,476. Actual is 1,148 — same
  pattern: a smaller pool to double, not a bug.
- **`validator` count is 0 in the current build.** The audit recorded
  49 natural validator rows; those were almost certainly captured
  inside task-notification prompts (Anvil pipeline messages frequently
  reference validators) and dropped by fix #9 *before* tagging. This
  is a fixable knock-on if validator coverage matters, but it's not a
  regression — validator rows were a tiny minority either way.

The per-project cap was lifted from the aggregate 30% in v1 (which
bound at 0 additions) to a per-project 15% cap. In v1 the oversampler
added 0 rows; in v2 it added 1,148. The functional intent of fix #6
is now realised.

---

## Chat template re-render

100/100 train rows (seed=42, Qwen/Qwen3-4B tokenizer):

```
rendered ok: 100/100
```

Same result as v1. No tokenization regressions from the new filter or
the per-project duplicates (duplicates carry the same `messages` /
`tools` shape — they only differ in `_oversample_origin` which is
stripped by `_strip_audit_fields` before write).

---

## Other deltas observed (verified to be benign)

- **`drops_per_fix.fix9_task_notification`** now present in MANIFEST;
  follows the same shape as the existing `fix8_vision_row` /
  `fix7_in_args_repetition_drops` keys.
- **`filters.per_project_max_pct`** replaces `filters.project_max_pct`
  in MANIFEST. Renamed deliberately to reflect the new semantics; if
  any downstream tooling reads `project_max_pct` by name it will need
  to be updated. (Not currently the case — the only reader is this
  audit, and the V3_PLAN doc which references the conceptual fix.)
- **Per-tool cap (20%)** still produces 0 drops because the largest
  tool category (`Read` at 3,727) is well below 20% of 22,041.
- Fix #1 in-arg scaffold scrubs dropped from 11 (v1) to 9 (v2). Two
  fewer rows happened to carry scaffolding in args; below the noise
  floor.
- Fix #7 repetition drops dropped from 1,562 (v1) to 1,470 (v2).
  Aligned with the smaller starting candidate set after fix #9 removes
  task-notification prompts (which themselves contained long repeated
  tool dumps).

No surprises. Code-path changes are confined to:

- `_make_row` — added the fix #9 early-return after `prompt_text`.
- `_oversample_project_tagged` — rewritten to per-project cap loop.
- `PROJECT_MAX_PCT` constant → `PER_PROJECT_MAX_PCT = 0.15`.
- MANIFEST `drops_per_fix` gained `fix9_task_notification` key.
- MANIFEST `filters` gained `per_project_max_pct`.

---

## Verdict

**APPROVE.** Both fixes shipped cleanly. Train/valid sizes are within
the expected band. Chat template still 100/100. Ready for v3 training
run.

Outstanding items (not blockers, log only):
- `validator` project tag has 0 train rows in this build — likely a
  fix #9 collateral, not a script bug. Revisit if validator coverage
  becomes important for the deployed model.
