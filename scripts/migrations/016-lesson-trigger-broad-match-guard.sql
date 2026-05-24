-- 016-lesson-trigger-broad-match-guard.sql
--
-- Forbid future "broad-match" input-triggered lessons — rows where
-- trigger_on='input' AND trigger_tool IS NULL AND trigger_pattern IS NULL.
-- Such a lesson matches every Edit/Write/Bash/NotebookEdit call and
-- dominates the per-tool-call systemMessage budget. The synth-lesson #86
-- (rule 'never X') is the canonical bad case; three legit cross-cutting
-- CRITICAL lessons (#35 read-before-edit, #36 docker-restart-zombie-cron,
-- #44 infra-dev-first) also fall in this set today and are left alone by
-- this migration per the agreed policy: existing rows keep firing, new
-- rows are refused at the DB layer.
--
-- The runtime fix (app/routes/lessons.py adding an
-- "trigger_tool IS NOT NULL OR trigger_pattern IS NOT NULL" clause for
-- input triggers) is the suspenders; this CHECK constraint is the belt.
-- They travel together — see commit history for context.
--
-- Idempotent: re-running on a DB that already has the constraint is a
-- no-op (the IF NOT EXISTS guard skips it).
--
-- Constraint stays NOT VALID intentionally — no companion .concurrent.sql
-- to run VALIDATE. There are existing active broad-match rows (#35, #36,
-- #44) that are legit cross-cutting CRITICAL safeguards; validating now
-- would fail. The constraint blocks future INSERTs and UPDATEs that would
-- create a new broad-match row, which is the goal. If those 3 existing
-- rows are ever narrowed (given a trigger_tool or trigger_pattern), the
-- operator can run `ALTER TABLE mem_lessons VALIDATE CONSTRAINT
-- chk_input_trigger_has_filter;` to convert the constraint to fully
-- enforced.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_input_trigger_has_filter'
          AND conrelid = 'public.mem_lessons'::regclass
    ) THEN
        ALTER TABLE mem_lessons
        ADD CONSTRAINT chk_input_trigger_has_filter
        CHECK (
            trigger_on <> 'input'
            OR trigger_tool IS NOT NULL
            OR trigger_pattern IS NOT NULL
        )
        NOT VALID;
    END IF;
END$$;

COMMIT;
