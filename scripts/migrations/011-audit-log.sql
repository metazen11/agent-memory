-- 011-audit-log.sql
-- Audit log for API operations.

BEGIN;

CREATE TABLE IF NOT EXISTS mem_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_name      TEXT,
    method          TEXT NOT NULL,
    path            TEXT NOT NULL,
    status_code     INTEGER,
    response_time_ms INTEGER,
    ip_address      TEXT
);

CREATE INDEX IF NOT EXISTS idx_mem_audit_log_ts ON mem_audit_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_mem_audit_log_agent ON mem_audit_log (agent_name, timestamp DESC);

COMMIT;
