-- Atomic replay receipts: acknowledge only after the entire write commits.
CREATE TABLE IF NOT EXISTS mem_ingest_receipts (
    route text NOT NULL,
    ingest_id text NOT NULL,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (route, ingest_id)
);
