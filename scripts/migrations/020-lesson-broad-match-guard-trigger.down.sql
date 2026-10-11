BEGIN;
DROP TRIGGER IF EXISTS trg_input_trigger_has_filter ON mem_lessons;
DROP FUNCTION IF EXISTS mem_lessons_input_trigger_has_filter();
ALTER TABLE mem_lessons ADD CONSTRAINT chk_input_trigger_has_filter
    CHECK (trigger_on <> 'input' OR trigger_tool IS NOT NULL OR trigger_pattern IS NOT NULL) NOT VALID;
COMMIT;
