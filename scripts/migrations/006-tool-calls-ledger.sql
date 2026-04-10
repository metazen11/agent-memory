-- 006-tool-calls-ledger.sql
-- Ensure tool-call ledger and queue backlinks exist for training exports.

BEGIN;

CREATE TABLE IF NOT EXISTS mem_tool_calls (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES mem_projects(id),
    queue_id INTEGER REFERENCES mem_observation_queue(id) ON DELETE SET NULL,
    observation_id INTEGER REFERENCES mem_observations(id) ON DELETE SET NULL,
    tool_name TEXT,
    tool_input JSONB,
    tool_response_preview TEXT,
    tool_success BOOLEAN,
    tool_error TEXT,
    prompt_text TEXT,
    cwd TEXT,
    source_system TEXT,
    source_mode TEXT,
    source_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE mem_tool_calls
    ADD COLUMN IF NOT EXISTS queue_id INTEGER REFERENCES mem_observation_queue(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS observation_id INTEGER REFERENCES mem_observations(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS tool_name TEXT,
    ADD COLUMN IF NOT EXISTS tool_input JSONB,
    ADD COLUMN IF NOT EXISTS tool_response_preview TEXT,
    ADD COLUMN IF NOT EXISTS tool_success BOOLEAN,
    ADD COLUMN IF NOT EXISTS tool_error TEXT,
    ADD COLUMN IF NOT EXISTS prompt_text TEXT,
    ADD COLUMN IF NOT EXISTS cwd TEXT,
    ADD COLUMN IF NOT EXISTS source_system TEXT,
    ADD COLUMN IF NOT EXISTS source_mode TEXT,
    ADD COLUMN IF NOT EXISTS source_agent TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE mem_observation_queue
    ADD COLUMN IF NOT EXISTS tool_call_id INTEGER REFERENCES mem_tool_calls(id);

CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_created ON mem_tool_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_project ON mem_tool_calls(project_id);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_session ON mem_tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_success ON mem_tool_calls(tool_success);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_observation ON mem_tool_calls(observation_id);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_tool_name ON mem_tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_mem_queue_tool_call_id ON mem_observation_queue(tool_call_id);

COMMIT;
