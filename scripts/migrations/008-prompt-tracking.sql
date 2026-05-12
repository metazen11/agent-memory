-- 008-prompt-tracking.sql
-- Upgrade mem_user_prompts to a first-class searchable entity.

BEGIN;

ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS project_id INTEGER REFERENCES mem_projects(id);
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS agent_name TEXT;
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS embedding_model_id INTEGER REFERENCES embedding_models(id);
ALTER TABLE mem_user_prompts ADD COLUMN IF NOT EXISTS tsv tsvector;

-- Backfill from sessions
UPDATE mem_user_prompts up SET project_id = s.project_id
FROM mem_sessions s WHERE up.session_id = s.id AND up.project_id IS NULL;

UPDATE mem_user_prompts up SET agent_name = s.agent_type
FROM mem_sessions s WHERE up.session_id = s.id AND up.agent_name IS NULL;

UPDATE mem_user_prompts SET tsv = to_tsvector('english', coalesce(prompt_text, ''))
WHERE tsv IS NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_mem_prompts_project ON mem_user_prompts (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_prompts_agent ON mem_user_prompts (agent_name);
CREATE INDEX IF NOT EXISTS idx_mem_prompts_created ON mem_user_prompts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_prompts_tsv ON mem_user_prompts USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_mem_prompts_embedding
    ON mem_user_prompts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMIT;
