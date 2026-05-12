-- 009-api-tokens.sql
-- API token authentication for agent-memory.

BEGIN;

CREATE TABLE IF NOT EXISTS mem_api_tokens (
    id              SERIAL PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,
    agent_name      TEXT NOT NULL,
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    created_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_mem_api_tokens_hash ON mem_api_tokens (token_hash) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_mem_api_tokens_agent ON mem_api_tokens (agent_name);

COMMIT;
