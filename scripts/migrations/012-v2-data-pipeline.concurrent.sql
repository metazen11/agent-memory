-- 012-v2-data-pipeline.concurrent.sql
-- Runs OUTSIDE any transaction. The migration runner detects the `.concurrent.sql`
-- suffix and applies these statements without an enclosing BEGIN/COMMIT block.
--
-- CREATE INDEX CONCURRENTLY cannot run inside a transaction.
-- VALIDATE CONSTRAINT is split out so we can interleave it with index build.
--
-- All statements are idempotent (IF NOT EXISTS / re-runnable VALIDATE).

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_tool_calls_prev_user_prompt_id_idx
    ON mem_tool_calls (prev_user_prompt_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_tool_calls_session_turn_idx
    ON mem_tool_calls (session_id, turn_index, turn_subindex);

-- VALIDATE CONSTRAINT is idempotent: once a NOT VALID FK has been validated,
-- pg_constraint.convalidated = true and subsequent VALIDATE calls are no-ops.
ALTER TABLE mem_tool_calls VALIDATE CONSTRAINT mem_tool_calls_prev_user_prompt_fk;
