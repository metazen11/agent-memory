-- agent-memory schema DDL
-- Run against existing Postgres (agentic database, wfhub user)
-- All tables prefixed with mem_ to avoid collision with existing tables

-- Ensure pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Embedding model registry (supports model switching)
CREATE TABLE IF NOT EXISTS embedding_models (
    id            SERIAL PRIMARY KEY,
    model_name    TEXT NOT NULL UNIQUE,
    dimensions    INTEGER NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'ollama',
    is_default    BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO embedding_models (model_name, dimensions, provider, is_default)
VALUES ('nomic-embed-text', 768, 'ollama', true)
ON CONFLICT (model_name) DO NOTHING;

-- Projects (auto-created from CWD basename)
CREATE TABLE IF NOT EXISTS mem_projects (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    full_path     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sessions
CREATE TABLE IF NOT EXISTS mem_sessions (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL UNIQUE,
    project_id      INTEGER NOT NULL REFERENCES mem_projects(id),
    agent_type      TEXT NOT NULL DEFAULT 'claude-code',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'completed', 'failed')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    summary         TEXT,
    prompt_count    INTEGER NOT NULL DEFAULT 0
);

-- Observations (core memory unit)
CREATE TABLE IF NOT EXISTS mem_observations (
    id                SERIAL PRIMARY KEY,
    session_id        INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    project_id        INTEGER NOT NULL REFERENCES mem_projects(id),

    -- Structured content (LLM-generated)
    title             TEXT NOT NULL,
    subtitle          TEXT,
    type              TEXT NOT NULL
                      CHECK (type IN ('decision','bugfix','feature','refactor',
                                      'discovery','change','pattern','gotcha')),
    narrative         TEXT,
    facts             JSONB DEFAULT '[]',
    concepts          JSONB DEFAULT '[]',
    files_read        JSONB DEFAULT '[]',
    files_modified    JSONB DEFAULT '[]',

    -- Raw text for re-embedding (NEVER lose this)
    raw_text          TEXT NOT NULL,

    -- Vector embedding
    embedding         vector(768),
    embedding_model_id INTEGER REFERENCES embedding_models(id),

    -- Metadata
    prompt_number     INTEGER,
    tool_name         TEXT,
    source_system     TEXT,
    source_mode       TEXT,
    source_agent      TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Full-text search (auto-maintained generated column)
    tsv               tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                        setweight(to_tsvector('english', coalesce(subtitle, '')), 'B') ||
                        setweight(to_tsvector('english', coalesce(narrative, '')), 'C') ||
                        setweight(to_tsvector('english', coalesce(raw_text, '')), 'D')
                      ) STORED
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_mem_obs_project ON mem_observations (project_id);
CREATE INDEX IF NOT EXISTS idx_mem_obs_type ON mem_observations (type);
CREATE INDEX IF NOT EXISTS idx_mem_obs_created ON mem_observations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_obs_tsv ON mem_observations USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_mem_obs_embedding ON mem_observations
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Observation queue (async processing, never blocks hooks)
CREATE TABLE IF NOT EXISTS mem_observation_queue (
    id                  SERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    tool_name           TEXT,
    tool_input          JSONB,
    tool_response_preview TEXT,
    cwd                 TEXT,
    last_user_message   TEXT,
    source_system       TEXT,
    source_mode         TEXT,
    source_agent        TEXT,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','done','failed','skipped')),
    retry_count         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mem_queue_status ON mem_observation_queue (status, created_at ASC);

-- User prompts (optional timeline)
CREATE TABLE IF NOT EXISTS mem_user_prompts (
    id              SERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES mem_sessions(id) ON DELETE CASCADE,
    prompt_number   INTEGER NOT NULL,
    prompt_text     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Tool call raw event capture (phase 1)
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

-- Tool call normalized ledger (phase 1)
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

ALTER TABLE mem_observation_queue
    ADD COLUMN IF NOT EXISTS tool_call_id INTEGER REFERENCES mem_tool_calls(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_mem_queue_tool_call_id
ON mem_observation_queue (tool_call_id);

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
