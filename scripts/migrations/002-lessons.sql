-- 002-lessons.sql
-- Lessons system: proactive rules that fire before risky operations.
-- Unlike observations (passive records), lessons are instructions that
-- prevent mistakes from recurring.

CREATE TABLE IF NOT EXISTS mem_lessons (
    id SERIAL PRIMARY KEY,
    project_id INTEGER REFERENCES mem_projects(id),  -- NULL = global (all projects)
    title TEXT NOT NULL,                              -- "Diff dev vs prod before Amplify deploy"
    rule TEXT NOT NULL,                               -- The actual instruction
    severity TEXT NOT NULL DEFAULT 'warning'
        CHECK (severity IN ('critical', 'warning', 'info')),

    -- Trigger conditions (when to fire via PreToolUse)
    trigger_tool TEXT,       -- Tool name match: "Bash", "Edit", NULL=any
    trigger_pattern TEXT,    -- Regex on tool_input: "amplify.*update-app", "DROP|ALTER"

    -- Context
    source_observation_id INTEGER REFERENCES mem_observations(id),  -- Which observation taught this

    -- Search
    embedding vector(768),
    raw_text TEXT NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(rule, '')), 'B')
    ) STORED,

    -- Tracking
    trigger_count INTEGER DEFAULT 0,
    last_triggered_at TIMESTAMPTZ,
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mem_lessons_project ON mem_lessons(project_id);
CREATE INDEX IF NOT EXISTS idx_mem_lessons_active ON mem_lessons(active) WHERE active = true;
CREATE INDEX IF NOT EXISTS idx_mem_lessons_trigger ON mem_lessons(trigger_tool) WHERE active = true;
CREATE INDEX IF NOT EXISTS idx_mem_lessons_tsv ON mem_lessons USING GIN(tsv);
