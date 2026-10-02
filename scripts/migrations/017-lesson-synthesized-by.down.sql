-- 017-lesson-synthesized-by.down.sql
--
-- Reverses migration 017 by dropping the index and the column.
--
-- Apply manually:
--   scripts/psql_wrapper.sh -f scripts/migrations/017-lesson-synthesized-by.down.sql
--
-- This file is NOT auto-applied by the runner (app/migrate.py skips
-- .down.sql). It exists for rollback only.
--
-- Destructive in one narrow sense: the provenance recorded so far is lost,
-- so locally-synthesized lessons become unfindable again. The lessons
-- themselves are untouched. Pair this with reverting the
-- app/lesson_distill.py INSERT, which writes the column.

BEGIN;

DROP INDEX IF EXISTS idx_mem_lessons_synthesized_by;

ALTER TABLE mem_lessons DROP COLUMN IF EXISTS synthesized_by;

COMMIT;
