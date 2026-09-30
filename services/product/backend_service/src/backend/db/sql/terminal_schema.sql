-- P0-FB-019 / C-TERMINAL-001: durable terminal record + outbox (Runtime Postgres).
-- Applied ONLY when TERMINAL_OUTCOMES_ENABLED is set (PostgresRuntimeStore.apply_terminal_schema);
-- runtime_schema.sql, and so every disabled deployment, is untouched.
-- Runtime Postgres is owned by this service alone (no tenant RLS here; the API
-- store is where tenant isolation is enforced). Idempotent.

CREATE TABLE IF NOT EXISTS terminal_records (
    terminal_record_id  TEXT PRIMARY KEY CHECK (terminal_record_id ~ '^tr:[0-9a-f]{64}$'),
    tenant_id           TEXT NOT NULL,
    business_session_id TEXT NOT NULL,
    runtime_session_id  TEXT NOT NULL,
    generation          TEXT NOT NULL,
    record_hash         TEXT NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    record              JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- At most one terminal record per identity generation; the first durable one wins.
    UNIQUE (tenant_id, business_session_id, generation)
);

CREATE TABLE IF NOT EXISTS terminal_outbox (
    terminal_record_id TEXT PRIMARY KEY REFERENCES terminal_records (terminal_record_id),
    -- Serialized once at insert and resent byte-for-byte (cleanup updates replace it whole).
    body               TEXT NOT NULL,
    body_sha256        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'delivered', 'rejected')),
    attempts           INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error         TEXT,
    delivered_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_terminal_outbox_due
    ON terminal_outbox (next_attempt_at) WHERE status = 'pending';
