-- 012-v2-data-pipeline.sql
-- V2 fine-tune data pipeline: schema additions for prompt <-> tool_call linkage.
--
-- Companion file: 012-v2-data-pipeline.concurrent.sql runs OUTSIDE a transaction
-- (the migration runner detects the suffix and skips the implicit BEGIN/COMMIT).
--
-- All new columns are nullable. The FK is added NOT VALID so the ALTER does not
-- need to scan the 54k-row mem_tool_calls table. VALIDATE CONSTRAINT happens in
-- the concurrent companion.
--
-- See docs/fine_tune/V2_DATA_PIPELINE_PLAN.md Step 1 for column rationale.

BEGIN;

-- mem_projects.git_remote: dedupes Dropbox-vs-local path forks of same project.
ALTER TABLE mem_projects   ADD COLUMN IF NOT EXISTS git_remote          text NULL;

-- Turn ordering: turn_index for chronological order within a session,
-- turn_subindex disambiguates multi-tool turns (N tool_use blocks in one
-- assistant message all share turn_index, differ in turn_subindex).
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS turn_index          int  NULL;
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS turn_subindex       int  NULL;

-- Linkage: the join column that fixes the prompt -> tool_call gap.
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS prev_user_prompt_id bigint NULL;

-- Backfill bookkeeping.
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS backfill_run_id     text NULL;
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS retention_class     text NULL DEFAULT 'live';
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS content_hash        text NULL;
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS truncated_at_bytes  int  NULL;

ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS retention_class   text NULL DEFAULT 'live';
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS backfill_run_id   text NULL;
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS turn_index        int  NULL;
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS content_hash      text NULL;

-- Foreign key: prev_user_prompt_id -> mem_user_prompts.id.
-- Added NOT VALID to avoid scanning the table; VALIDATE happens in concurrent file.
-- Guarded by a check so re-applying the SQL block stays idempotent.
DO $mig$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'mem_tool_calls'
          AND c.conname = 'mem_tool_calls_prev_user_prompt_fk'
    ) THEN
        ALTER TABLE mem_tool_calls
            ADD CONSTRAINT mem_tool_calls_prev_user_prompt_fk
            FOREIGN KEY (prev_user_prompt_id)
            REFERENCES mem_user_prompts(id) ON DELETE SET NULL NOT VALID;
    END IF;
END
$mig$;

COMMIT;
