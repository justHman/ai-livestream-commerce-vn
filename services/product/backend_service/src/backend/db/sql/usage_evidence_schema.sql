-- P0-FB-017 / C-USAGE-EVIDENCE-001: durable signed usage-evidence outbox (Runtime Postgres).
-- Applied ONLY when USAGE_EVIDENCE_ENABLED is set (PostgresRuntimeStore.apply_usage_evidence_schema);
-- runtime_schema.sql, and so every disabled deployment, is untouched. Idempotent.

-- One counter row per execution identity. Taken FOR UPDATE inside the ready-flip
-- transaction, so usage_sequence is gap-free across concurrent writers/replicas.
CREATE TABLE IF NOT EXISTS usage_evidence_sequence (
    tenant_id           TEXT NOT NULL,
    business_session_id TEXT NOT NULL,
    runtime_session_id  TEXT NOT NULL,
    generation          TEXT NOT NULL,
    last_usage_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_usage_sequence >= 0),
    PRIMARY KEY (tenant_id, business_session_id, runtime_session_id, generation)
);

CREATE TABLE IF NOT EXISTS usage_evidence_outbox (
    event_id            TEXT PRIMARY KEY CHECK (char_length(event_id) <= 255),
    event_type          TEXT NOT NULL CHECK (event_type IN ('ai.execution.usage_evidence', 'ai.usage.reported')),
    tenant_id           TEXT NOT NULL CHECK (tenant_id ~* '^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$'),
    business_session_id TEXT NOT NULL CHECK (char_length(business_session_id) <= 255),
    runtime_session_id  TEXT NOT NULL CHECK (char_length(runtime_session_id) <= 255),
    generation          TEXT NOT NULL CHECK (char_length(generation) <= 255),
    kind                TEXT NOT NULL,
    interval_id         TEXT NOT NULL,
    -- NULL for vendor COGS, a staged row (numbered only when it becomes ready) and a discarded row.
    usage_sequence      BIGINT CHECK (usage_sequence > 0),
    -- Execution evidence sequence that caused the row; NULL for command-derived rows.
    execution_sequence  BIGINT,
    -- Execution state sequence after the causing fact applied: the sweeper's proof.
    applied_sequence    BIGINT NOT NULL DEFAULT 0,
    -- One unique token per staging ATTEMPT; the committed proof in the session meta lists
    -- tokens, so it identifies the exact stored payload that committed.
    stage_token         TEXT,
    -- Session commit version this attempt would commit at (commit order for numbering).
    staged_version      BIGINT NOT NULL DEFAULT 0,
    occurred_at         TIMESTAMPTZ NOT NULL,
    -- Serialized once at insert and resent byte-for-byte on every attempt.
    body                BYTEA NOT NULL CHECK (octet_length(body) <= 1048576),
    body_sha256         TEXT NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    status              TEXT NOT NULL DEFAULT 'staged'
                        CHECK (status IN ('staged', 'ready', 'delivered', 'conflict', 'rejected', 'discarded')),
    attempts            INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_token         UUID,
    last_status         INT,
    last_error          TEXT,
    delivered_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, business_session_id, runtime_session_id, generation, kind, interval_id),
    UNIQUE (tenant_id, business_session_id, runtime_session_id, generation, usage_sequence)
);

CREATE INDEX IF NOT EXISTS idx_usage_evidence_outbox_due
    ON usage_evidence_outbox (next_attempt_at) WHERE status = 'ready';
CREATE INDEX IF NOT EXISTS idx_usage_evidence_outbox_staged
    ON usage_evidence_outbox (created_at) WHERE status = 'staged';

-- Idempotent upgrade for a database created before these columns existed.
ALTER TABLE usage_evidence_outbox ADD COLUMN IF NOT EXISTS stage_token TEXT;
ALTER TABLE usage_evidence_outbox ADD COLUMN IF NOT EXISTS staged_version BIGINT NOT NULL DEFAULT 0;
