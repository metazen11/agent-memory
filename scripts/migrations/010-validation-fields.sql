-- 010-validation-fields.sql
-- Add confidence_score, verified, source_agent to observations.

BEGIN;

ALTER TABLE mem_observations ADD COLUMN IF NOT EXISTS confidence_score FLOAT;
ALTER TABLE mem_observations ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE mem_observations ADD COLUMN IF NOT EXISTS source_agent TEXT;

CREATE INDEX IF NOT EXISTS idx_mem_observations_verified ON mem_observations (verified) WHERE verified = true;
CREATE INDEX IF NOT EXISTS idx_mem_observations_source_agent ON mem_observations (source_agent);

COMMIT;
