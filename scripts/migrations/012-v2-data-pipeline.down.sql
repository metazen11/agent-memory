-- 012-v2-data-pipeline.down.sql
-- Reverses migration 012. Drops indexes, FK, then columns.
--
-- Indexes drop outside a transaction (CONCURRENTLY) for parity with the up
-- migration's concurrent index build; columns + FK drop inside the BEGIN.
--
-- Apply manually:
--   psql -U mz -d agent_memory -f scripts/migrations/012-v2-data-pipeline.down.sql
--
-- This file is NOT auto-applied by the runner. It exists for rollback only.
-- All new columns are nullable so the drop is non-destructive (no data loss
-- beyond the new columns themselves).

DROP INDEX IF EXISTS mem_tool_calls_prev_user_prompt_id_idx;
DROP INDEX IF EXISTS mem_tool_calls_session_turn_idx;

BEGIN;

ALTER TABLE mem_tool_calls
    DROP CONSTRAINT IF EXISTS mem_tool_calls_prev_user_prompt_fk;

ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS truncated_at_bytes;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS content_hash;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS retention_class;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS backfill_run_id;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS prev_user_prompt_id;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS turn_subindex;
ALTER TABLE mem_tool_calls   DROP COLUMN IF EXISTS turn_index;

ALTER TABLE mem_user_prompts DROP COLUMN IF EXISTS content_hash;
ALTER TABLE mem_user_prompts DROP COLUMN IF EXISTS turn_index;
ALTER TABLE mem_user_prompts DROP COLUMN IF EXISTS backfill_run_id;
ALTER TABLE mem_user_prompts DROP COLUMN IF EXISTS retention_class;

ALTER TABLE mem_projects     DROP COLUMN IF EXISTS git_remote;

-- Remove the migration tracking row so the runner re-applies on next startup.
DELETE FROM mem_schema_migrations WHERE filename IN (
    '012-v2-data-pipeline.sql',
    '012-v2-data-pipeline.concurrent.sql'
);

COMMIT;
