-- 017-lesson-synthesized-by.sql
--
-- Record WHICH model synthesized each distilled lesson.
--
-- Motivation: the distiller (app/lesson_distill.py) prefers Anthropic and
-- falls back to a local GGUF 7B when the API call fails. When the
-- configured key ran out of credit, every lesson in that run was written
-- locally instead — and the local model's output is markedly worse. The
-- canonical bad case it produced was "Set replace_all=True in edit_file
-- calls": too thin to be actionable AND wrong advice (blind replace_all
-- makes incorrect edits; the real fix is more surrounding context).
--
-- Because nothing on the row said who wrote it, those low-quality lessons
-- were indistinguishable from good ones after the fact and could not be
-- found for re-synthesis once billing was restored. This column makes
-- them queryable:
--
--     SELECT id, title FROM mem_lessons
--     WHERE synthesized_by LIKE 'local:%' AND active = true;
--
-- Values are provider TIERS, not model version ids (e.g.
-- 'anthropic:claude-haiku-4-5', 'local:gguf') — see PROVIDER_ANTHROPIC /
-- PROVIDER_LOCAL in app/lesson_distill.py. Re-synthesis only needs to know
-- the tier, and a tier string does not churn on every model release.
--
-- Nullable with no default and no backfill: NULL means "unknown", which is
-- the honest value for the hand-written and pre-017 rows. Distinguishing
-- "we don't know" from "a local model wrote it" matters here — assuming
-- the latter would flag every hand-written lesson for re-synthesis.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS make a
-- second run a no-op.
--
-- No companion .concurrent.sql: this adds a nullable column with no
-- default and no constraint, so Postgres takes only a brief ACCESS
-- EXCLUSIVE lock for the catalog update and rewrites nothing. There is no
-- constraint to validate, hence nothing to defer with NOT VALID. The
-- partial index is small (mem_lessons has ~100 rows) and builds instantly.

BEGIN;

ALTER TABLE mem_lessons ADD COLUMN IF NOT EXISTS synthesized_by TEXT;

COMMENT ON COLUMN mem_lessons.synthesized_by IS
    'Provider tier that synthesized this lesson (e.g. anthropic:claude-haiku-4-5, '
    'local:gguf). NULL = unknown / hand-written. Used to find low-quality '
    'locally-synthesized lessons for re-synthesis.';

-- Partial index: the only query this column serves is "find the lessons a
-- weaker model wrote so I can redo them", which always filters to active
-- rows with a known provider.
CREATE INDEX IF NOT EXISTS idx_mem_lessons_synthesized_by
    ON mem_lessons (synthesized_by)
    WHERE active = true AND synthesized_by IS NOT NULL;

COMMIT;
