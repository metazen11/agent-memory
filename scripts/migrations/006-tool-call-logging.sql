-- 006-tool-call-logging.sql
-- Phase 1 tool-call logging:
-- - raw event capture for replay/debug
-- - normalized tool-call rows for querying/reporting
-- - queue linkage + human-readable session history view

BEGIN;

-- Raw hook/event payloads (append-only)
CREATE TABLE IF NOT EXISTS mem_tool_call_events (
    id                  SERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    queue_id            INTEGER REFERENCES mem_observation_queue(id) ON DELETE SET NULL,
    hook_event_name     TEXT,
    tool_name           TEXT,
    tool_input          JSONB,
    tool_response       JSONB,
    tool_success        BOOLEAN,
    tool_error          TEXT,
    prompt_text         TEXT,
    cwd                 TEXT,
    source_system       TEXT,
    source_mode         TEXT,
    source_agent        TEXT,
    raw_event_json      JSON,
    raw_event_jsonb     JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mem_tool_call_events_session_created
ON mem_tool_call_events (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_tool_call_events_tool_name
ON mem_tool_call_events (tool_name);
CREATE INDEX IF NOT EXISTS idx_mem_tool_call_events_success
ON mem_tool_call_events (tool_success);
CREATE INDEX IF NOT EXISTS idx_mem_tool_call_events_source_agent
ON mem_tool_call_events (source_agent);

-- Normalized per-tool-call rows (query-friendly)
CREATE TABLE IF NOT EXISTS mem_tool_calls (
    id                    SERIAL PRIMARY KEY,
    event_id              INTEGER REFERENCES mem_tool_call_events(id) ON DELETE SET NULL,
    session_id            INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    project_id            INTEGER NOT NULL REFERENCES mem_projects(id),
    queue_id              INTEGER REFERENCES mem_observation_queue(id) ON DELETE SET NULL,
    observation_id        INTEGER REFERENCES mem_observations(id) ON DELETE SET NULL,
    hook_event_name       TEXT,
    tool_name             TEXT,
    tool_input            JSONB,
    tool_response_preview TEXT,
    tool_success          BOOLEAN,
    tool_error            TEXT,
    prompt_text           TEXT,
    cwd                   TEXT,
    queue_status          TEXT NOT NULL DEFAULT 'pending'
                          CHECK (queue_status IN ('pending','processing','done','failed','skipped')),
    source_system         TEXT,
    source_mode           TEXT,
    source_agent          TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_session_created
ON mem_tool_calls (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_status_created
ON mem_tool_calls (queue_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_tool_name
ON mem_tool_calls (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_success
ON mem_tool_calls (tool_success);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_source_agent
ON mem_tool_calls (source_agent);
CREATE INDEX IF NOT EXISTS idx_mem_tool_calls_observation
ON mem_tool_calls (observation_id);

-- Link queue rows to normalized tool calls
ALTER TABLE mem_observation_queue
    ADD COLUMN IF NOT EXISTS tool_call_id INTEGER;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'mem_observation_queue_tool_call_id_fkey'
    ) THEN
        ALTER TABLE mem_observation_queue
            ADD CONSTRAINT mem_observation_queue_tool_call_id_fkey
            FOREIGN KEY (tool_call_id) REFERENCES mem_tool_calls(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_mem_queue_tool_call_id
ON mem_observation_queue (tool_call_id);

-- Spreadsheet-like session timeline view for tool history
CREATE OR REPLACE VIEW mem_v_session_tool_history AS
SELECT
    tc.id AS tool_call_id,
    s.id AS session_db_id,
    s.session_id AS session_id,
    p.id AS project_id,
    p.name AS project_name,
    p.full_path AS project_path,
    s.agent_type AS session_agent_type,
    COALESCE(tc.source_agent, tc.source_system, s.agent_type) AS calling_agent,
    tc.source_system,
    tc.source_mode,
    tc.hook_event_name,
    tc.created_at AS tool_called_at,
    COALESCE(tc.prompt_text, q.last_user_message, up.prompt_text) AS prompt_text,
    tc.tool_name,
    tc.tool_input,
    tc.tool_response_preview,
    tc.tool_success,
    tc.tool_error,
    tc.queue_status,
    tc.processed_at,
    o.id AS observation_id,
    o.type AS observation_type,
    o.title AS observation_title,
    o.created_at AS observation_created_at
FROM mem_tool_calls tc
JOIN mem_sessions s ON s.id = tc.session_id
JOIN mem_projects p ON p.id = tc.project_id
LEFT JOIN mem_observation_queue q ON q.id = tc.queue_id
LEFT JOIN mem_observations o ON o.id = tc.observation_id
LEFT JOIN LATERAL (
    SELECT up.prompt_text
    FROM mem_user_prompts up
    WHERE up.session_id = s.id
      AND up.created_at <= tc.created_at
    ORDER BY up.created_at DESC
    LIMIT 1
) up ON TRUE
ORDER BY tc.created_at DESC;

COMMIT;
