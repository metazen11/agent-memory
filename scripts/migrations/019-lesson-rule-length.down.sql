-- 019-lesson-rule-length.down.sql
--
-- Reverses migration 019. NOT auto-applied (app/migrate.py skips .down.sql).
--   scripts/psql_wrapper.sh -f scripts/migrations/019-lesson-rule-length.down.sql
--
-- Destructive: drops `detail`, i.e. the original long-form text of every
-- condensed lesson. Restore rules first from the mem_lessons_backup_<ts>
-- table written by scripts/condense_lessons.py --apply if you need them.

BEGIN;

ALTER TABLE mem_lessons DROP CONSTRAINT IF EXISTS chk_lesson_rule_len;
ALTER TABLE mem_lessons DROP COLUMN IF EXISTS legacy_long_rule;
-- tsv depends on detail: restore the 002 expression first.
DROP INDEX IF EXISTS idx_mem_lessons_tsv;
ALTER TABLE mem_lessons DROP COLUMN IF EXISTS tsv;
ALTER TABLE mem_lessons DROP COLUMN IF EXISTS detail;
ALTER TABLE mem_lessons ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(rule, '')), 'B')
) STORED;
CREATE INDEX idx_mem_lessons_tsv ON mem_lessons USING GIN(tsv);

COMMIT;
