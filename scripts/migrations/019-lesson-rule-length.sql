-- 019-lesson-rule-length.sql  (issue #75)
--
-- Cap mem_lessons.rule at 280 characters and keep the long-form story in a
-- new `detail` column.
--
-- Why: user-prompt-submit injects each active CRITICAL lesson's `rule`
-- capped at 280 chars (PR #74). 18 of 20 active critical lessons were
-- 297-2,178 chars and most put the actual instruction AFTER the incident
-- narrative, so the cap cut off the "what to do". The writers
-- (POST /api/lessons, MCP create_lesson, the distiller) now condense long
-- rules via app/lesson_condense.py and keep the original in `detail`; this
-- constraint is the database-level guarantee that nothing bypasses them.
--
-- Why the `legacy_long_rule` flag instead of a bare CHECK ... NOT VALID:
-- NOT VALID only skips the scan of existing rows when the constraint is
-- ADDED. Postgres still checks every NEW row version, and an UPDATE of
-- ANY column produces a new row version — so with a bare NOT VALID check,
-- `UPDATE mem_lessons SET trigger_count = trigger_count + 1` (the
-- trigger-tracking endpoint) and deactivation would start failing on every
-- long legacy lesson the moment this migration ran. Verified on PG:
--   ERROR: new row for relation "t" violates check constraint "c"
-- The flag grandfathers exactly the rows that were over the limit when this
-- ran; new rows default to false and are bound by the 280 cap.
-- scripts/condense_lessons.py --apply clears the flag as it condenses each
-- row, and --validate-constraint then VALIDATEs the constraint.
--
-- Idempotent: IF NOT EXISTS guards + a catalog check around ADD CONSTRAINT.
-- Brief ACCESS EXCLUSIVE lock; mem_lessons is ~hundreds of rows.

BEGIN;

ALTER TABLE mem_lessons ADD COLUMN IF NOT EXISTS detail TEXT;
ALTER TABLE mem_lessons
    ADD COLUMN IF NOT EXISTS legacy_long_rule BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN mem_lessons.detail IS
    'Long-form context (incident story, rationale). Not injected into prompts; '
    'the injected instruction is `rule` (<= 280 chars).';
COMMENT ON COLUMN mem_lessons.legacy_long_rule IS
    'TRUE only for rows whose rule already exceeded 280 chars when migration 019 '
    'ran. Cleared by scripts/condense_lessons.py --apply. New rows: always false.';

UPDATE mem_lessons
   SET legacy_long_rule = true
 WHERE char_length(rule) > 280
   AND NOT legacy_long_rule;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_lesson_rule_len'
           AND conrelid = 'mem_lessons'::regclass
    ) THEN
        ALTER TABLE mem_lessons
            ADD CONSTRAINT chk_lesson_rule_len
            CHECK (char_length(rule) <= 280 OR legacy_long_rule)
            NOT VALID;
    END IF;
END $$;

COMMIT;
