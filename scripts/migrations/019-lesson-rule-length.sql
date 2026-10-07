-- 019-lesson-rule-length.sql  (issue #75)
--
-- Cap mem_lessons.rule at 280 characters and keep the long-form story in a
-- new `detail` column.
--
-- Why: user-prompt-submit injects each active CRITICAL lesson's `rule`
-- capped at 280 chars (PR #74). Most long rules put the actual instruction
-- AFTER the incident narrative, so the cap cut off the "what to do". The
-- writers (POST/PATCH /api/lessons, MCP create_lesson, the distiller) now
-- condense long rules via app/lesson_condense.py and keep the original in
-- `detail`. This migration is the database-level guarantee.
--
-- End state: CHECK (char_length(rule) <= 280), validated. Nothing else.
--
-- Transition while pre-019 long rules still exist
-- -----------------------------------------------
-- A CHECK cannot be added yet. Not even NOT VALID: Postgres checks every
-- NEW row version on UPDATE, so `SET trigger_count = trigger_count + 1`
-- and deactivation on a long legacy row would fail immediately (reproduced
-- on PG16). A grandfather flag column was tried and rejected because it was
-- writable: any INSERT/UPDATE could set it and store a long rule.
--
-- Instead, a BEFORE INSERT OR UPDATE trigger refuses every write that could
-- produce a long ACTIVE-able rule:
--   * any INSERT with a rule > 280;
--   * any UPDATE that changes `rule` to > 280;
--   * any UPDATE that reactivates a row whose rule is > 280;
--   * any UPDATE that promotes such a row to severity 'critical' (active
--     critical lessons are the set user-prompt-submit injects).
-- Other updates of a long legacy row (counters, deactivation) still pass.
-- No state can be written to bypass it.
--
-- scripts/condense_lessons.py condenses ALL long rows (active and
-- inactive). Its --validate-constraint step then calls
-- mem_lessons_finalize_rule_cap(), which adds the validated CHECK and drops
-- the trigger. The CHECK add fails while any long row remains. On a
-- database with no long rows (fresh install, CI) this migration finalizes
-- immediately.
--
-- Idempotent: CREATE OR REPLACE / IF [NOT] EXISTS throughout, and the
-- trigger is only (re)created while the CHECK does not exist yet.

BEGIN;

ALTER TABLE mem_lessons ADD COLUMN IF NOT EXISTS detail TEXT;

COMMENT ON COLUMN mem_lessons.detail IS
    'Long-form context (incident story, rationale). Not injected into prompts; '
    'the injected instruction is `rule` (<= 280 chars).';

-- Full-text search must still find a lesson by its backstory once that
-- text moves from `rule` to `detail`. `tsv` is a generated column (002), so
-- its expression can only change by drop + re-add (rewrites a tiny table).
DROP INDEX IF EXISTS idx_mem_lessons_tsv;
ALTER TABLE mem_lessons DROP COLUMN IF EXISTS tsv;
ALTER TABLE mem_lessons ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(rule, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(detail, '')), 'C')
) STORED;
CREATE INDEX idx_mem_lessons_tsv ON mem_lessons USING GIN(tsv);

CREATE OR REPLACE FUNCTION mem_lessons_enforce_rule_cap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF char_length(NEW.rule) > 280 AND (
        TG_OP = 'INSERT'
        OR NEW.rule IS DISTINCT FROM OLD.rule
        OR (NEW.active AND NOT OLD.active)
        OR (NEW.severity = 'critical' AND OLD.severity IS DISTINCT FROM 'critical')
    ) THEN
        RAISE EXCEPTION 'lesson rule is % chars (max 280); condense it and put the backstory in detail',
            char_length(NEW.rule)
            USING ERRCODE = 'check_violation', CONSTRAINT = 'chk_lesson_rule_len';
    END IF;
    RETURN NEW;
END $$;

-- Swap the transition trigger for the final CHECK. Raises check_violation
-- (and changes nothing) while any rule is still over 280 chars.
CREATE OR REPLACE FUNCTION mem_lessons_finalize_rule_cap() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_lesson_rule_len'
           AND conrelid = 'mem_lessons'::regclass
    ) THEN
        ALTER TABLE mem_lessons
            ADD CONSTRAINT chk_lesson_rule_len CHECK (char_length(rule) <= 280);
    END IF;
    DROP TRIGGER IF EXISTS trg_mem_lessons_rule_cap ON mem_lessons;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_lesson_rule_len'
           AND conrelid = 'mem_lessons'::regclass
    ) THEN
        DROP TRIGGER IF EXISTS trg_mem_lessons_rule_cap ON mem_lessons;
        CREATE TRIGGER trg_mem_lessons_rule_cap
            BEFORE INSERT OR UPDATE ON mem_lessons
            FOR EACH ROW EXECUTE FUNCTION mem_lessons_enforce_rule_cap();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM mem_lessons WHERE char_length(rule) > 280) THEN
        PERFORM mem_lessons_finalize_rule_cap();
    END IF;
END $$;

COMMIT;
