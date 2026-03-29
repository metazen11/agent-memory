-- Backfill progress tracking table
CREATE TABLE IF NOT EXISTS backfill_log (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    jsonl_path      TEXT NOT NULL,
    total_tools     INTEGER DEFAULT 0,
    processed       INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    last_processed_idx INTEGER DEFAULT 0,    -- resume point within session
    status          TEXT DEFAULT 'pending',  -- pending|in_progress|done|failed
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    UNIQUE(session_id)
);
