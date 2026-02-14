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
