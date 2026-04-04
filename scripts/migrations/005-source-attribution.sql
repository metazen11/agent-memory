-- Optional source attribution for queue payloads and stored observations.
-- Nullable for backward compatibility with older clients.

ALTER TABLE mem_observation_queue
    ADD COLUMN IF NOT EXISTS source_system TEXT,
    ADD COLUMN IF NOT EXISTS source_mode TEXT,
    ADD COLUMN IF NOT EXISTS source_agent TEXT;

ALTER TABLE mem_observations
    ADD COLUMN IF NOT EXISTS source_system TEXT,
    ADD COLUMN IF NOT EXISTS source_mode TEXT,
    ADD COLUMN IF NOT EXISTS source_agent TEXT;

-- Backfill source_system from the session agent type when it is missing.
UPDATE mem_observations o
SET source_system = s.agent_type
FROM mem_sessions s
WHERE o.session_id = s.id
  AND o.source_system IS NULL
  AND s.agent_type IS NOT NULL;
