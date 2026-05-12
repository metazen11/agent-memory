-- 007-lesson-trigger-expansion.sql
-- Expand lesson triggers: output matching, phase triggers, file-scope triggers.
-- Backward compatible — all new columns nullable, trigger_on defaults to 'input'.

ALTER TABLE mem_lessons
    ADD COLUMN IF NOT EXISTS trigger_on TEXT NOT NULL DEFAULT 'input'
        CHECK (trigger_on IN ('input', 'output', 'phase', 'file_scope'));

ALTER TABLE mem_lessons
    ADD COLUMN IF NOT EXISTS trigger_output_pattern TEXT;

ALTER TABLE mem_lessons
    ADD COLUMN IF NOT EXISTS trigger_phase TEXT
        CHECK (trigger_phase IS NULL OR trigger_phase IN (
            'pre_tool', 'post_tool', 'pre_response', 'session_end'
        ));

ALTER TABLE mem_lessons
    ADD COLUMN IF NOT EXISTS trigger_files TEXT[];

-- Validate: output lessons must have trigger_output_pattern
ALTER TABLE mem_lessons
    ADD CONSTRAINT chk_output_has_pattern
        CHECK (trigger_on != 'output' OR trigger_output_pattern IS NOT NULL);

-- Validate: phase lessons must have trigger_phase
ALTER TABLE mem_lessons
    ADD CONSTRAINT chk_phase_has_phase
        CHECK (trigger_on != 'phase' OR trigger_phase IS NOT NULL);

-- Validate: file_scope lessons must have trigger_files
ALTER TABLE mem_lessons
    ADD CONSTRAINT chk_files_has_files
        CHECK (trigger_on != 'file_scope' OR trigger_files IS NOT NULL);

-- Validate: trigger_output_pattern is reasonable length
ALTER TABLE mem_lessons
    ADD CONSTRAINT chk_output_pattern_len
        CHECK (trigger_output_pattern IS NULL OR length(trigger_output_pattern) <= 500);

-- Indexes for new trigger types
CREATE INDEX IF NOT EXISTS idx_lessons_trigger_on
    ON mem_lessons (trigger_on) WHERE active = true;

CREATE INDEX IF NOT EXISTS idx_lessons_phase
    ON mem_lessons (trigger_phase) WHERE active = true AND trigger_on = 'phase';
