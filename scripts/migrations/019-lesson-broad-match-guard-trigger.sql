-- 019-lesson-broad-match-guard-trigger.sql
--
-- Replace 016's NOT VALID CHECK with a trigger. NOT VALID only skips
-- existing rows at ADD time; Postgres still checks every later UPDATE, so
-- bumping trigger_count on the legacy broad-match lessons (016 left them
-- active on purpose) raised CheckViolationError -> 500 on
-- POST /api/lessons/{id}/trigger. The trigger enforces the same rule only
-- on INSERT or when trigger_on/trigger_tool/trigger_pattern change.

BEGIN;

ALTER TABLE mem_lessons DROP CONSTRAINT IF EXISTS chk_input_trigger_has_filter;

CREATE OR REPLACE FUNCTION mem_lessons_input_trigger_has_filter() RETURNS trigger AS $$
BEGIN
    IF NEW.trigger_on = 'input'
       AND NEW.trigger_tool IS NULL
       AND NEW.trigger_pattern IS NULL THEN
        RAISE EXCEPTION 'input-triggered lesson needs trigger_tool or trigger_pattern'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_input_trigger_has_filter ON mem_lessons;
CREATE TRIGGER trg_input_trigger_has_filter
    BEFORE INSERT OR UPDATE OF trigger_on, trigger_tool, trigger_pattern
    ON mem_lessons
    FOR EACH ROW EXECUTE FUNCTION mem_lessons_input_trigger_has_filter();

COMMIT;
