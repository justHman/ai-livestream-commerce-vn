-- Runtime DB schema (owner: livestream control plane)
-- Postgres 16+. Extensions optional for later waves (pgvector, pg_trgm).
-- No live DB required for unit tests; this file is the source of truth.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Active/ended livestream sessions (runtime state, not business users/shops).
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'created',
    mode            TEXT NOT NULL DEFAULT 'mock',
    render_backend  TEXT,
    avatar_id       TEXT,
    room_name       TEXT,
    owner_instance  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions (created_at);

-- Frozen product snapshot attached to a session (anti-coupling with business DB).
CREATE TABLE IF NOT EXISTS session_products (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    product_id      TEXT NOT NULL,
    name            TEXT,
    price           NUMERIC(14, 2),
    currency        TEXT DEFAULT 'VND',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    sort_order      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_session_products_session_id ON session_products (session_id);

-- Ingested viewer / platform chat messages.
CREATE TABLE IF NOT EXISTS viewer_msgs (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    comment_id      TEXT,
    author          TEXT,
    text            TEXT NOT NULL,
    source          TEXT DEFAULT 'platform',
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_viewer_msgs_session_id ON viewer_msgs (session_id);
CREATE INDEX IF NOT EXISTS idx_viewer_msgs_session_received ON viewer_msgs (session_id, received_at);

-- Director FSM decisions (action, product, score, phase cursor).
CREATE TABLE IF NOT EXISTS director_decisions (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    product_id      TEXT,
    score           DOUBLE PRECISION,
    phase           TEXT,
    product_idx     INT,
    talking_point_idx INT,
    utterance       TEXT,
    reason          TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_director_decisions_session_id ON director_decisions (session_id);
CREATE INDEX IF NOT EXISTS idx_director_decisions_session_created
    ON director_decisions (session_id, created_at);

-- LLM call telemetry (latency, tokens, model).
CREATE TABLE IF NOT EXISTS llm_call_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT REFERENCES sessions (session_id) ON DELETE SET NULL,
    engine          TEXT,
    model           TEXT,
    prompt_tokens   INT,
    completion_tokens INT,
    latency_ms      INT,
    status          TEXT,
    error           TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_call_log_session_id ON llm_call_log (session_id);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_created_at ON llm_call_log (created_at);

-- TTS call telemetry.
CREATE TABLE IF NOT EXISTS tts_call_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT REFERENCES sessions (session_id) ON DELETE SET NULL,
    engine          TEXT,
    model           TEXT,
    sample_rate     INT,
    duration_ms     INT,
    latency_ms      INT,
    status          TEXT,
    error           TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tts_call_log_session_id ON tts_call_log (session_id);
CREATE INDEX IF NOT EXISTS idx_tts_call_log_created_at ON tts_call_log (created_at);

-- Universal commerce entity documents (task 8.4): JSONB document per entity,
-- revisioned; upserts must not regress revisions (guard lives in SQL).
CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    revision        INT NOT NULL DEFAULT 0,
    document        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (entity_type);

-- Ops / security audit trail.
CREATE TABLE IF NOT EXISTS audit_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT,
    actor           TEXT,
    event_type      TEXT NOT NULL,
    resource        TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_session_id ON audit_events (session_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events (event_type);
CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events (created_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- Script Authoring (OpenSpec Change B: approved-script-authoring-pipeline).
-- Additive (CREATE IF NOT EXISTS) so `apply_schema` is idempotent. Aggregate
-- string ids (`<prefix>:<32hex>`), TEXT enums (`StrEnum.value`), TIMESTAMPTZ.
-- Immutable rows (script_versions / script_segments) are never UPDATE'd; the
-- current/approved pointers live on script_items only.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS script_sets (
    id          TEXT PRIMARY KEY,
    shop_id     TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    brief       JSONB NOT NULL DEFAULT '{}'::jsonb,
    product_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    session_id  TEXT,
    revision    INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS script_items (
    id                 TEXT PRIMARY KEY,
    script_set_id      TEXT NOT NULL REFERENCES script_sets(id) ON DELETE CASCADE,
    product_id         TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'empty',
    source             TEXT,
    current_version_id TEXT,
    approved_version_id TEXT,
    intent             TEXT,
    revision           INT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_script_item_set_product UNIQUE (script_set_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_script_items_script_set_id ON script_items (script_set_id);
CREATE INDEX IF NOT EXISTS idx_script_items_approved_version_id ON script_items (approved_version_id);

CREATE TABLE IF NOT EXISTS script_versions (
    id                 TEXT PRIMARY KEY,
    script_item_id     TEXT NOT NULL REFERENCES script_items(id) ON DELETE CASCADE,
    version            INT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'draft',
    source             TEXT NOT NULL DEFAULT 'manual',
    display_text       TEXT NOT NULL DEFAULT '',
    spoken_text        TEXT NOT NULL DEFAULT '',
    text_hash          TEXT NOT NULL DEFAULT '',
    segment_version_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    plan_version       INT NOT NULL DEFAULT 1,
    gate_run_id        TEXT,
    fingerprint        JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_script_version_item_version UNIQUE (script_item_id, version)
);

CREATE INDEX IF NOT EXISTS idx_script_versions_item_text_hash
    ON script_versions (script_item_id, text_hash);

CREATE TABLE IF NOT EXISTS script_gate_runs (
    id                  TEXT PRIMARY KEY,
    script_item_id      TEXT NOT NULL REFERENCES script_items(id) ON DELETE CASCADE,
    is_full             BOOLEAN NOT NULL DEFAULT TRUE,
    passed              BOOLEAN NOT NULL DEFAULT FALSE,
    rule_set_fingerprint TEXT NOT NULL DEFAULT '',
    script_version_id   TEXT,
    violations          JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_script_gate_runs_item_id ON script_gate_runs (script_item_id);
CREATE INDEX IF NOT EXISTS idx_script_gate_runs_version_id ON script_gate_runs (script_version_id);

CREATE TABLE IF NOT EXISTS script_approvals (
    id               TEXT PRIMARY KEY,
    script_item_id   TEXT NOT NULL REFERENCES script_items(id) ON DELETE CASCADE,
    script_version_id TEXT NOT NULL REFERENCES script_versions(id),
    actor            TEXT NOT NULL,
    approval_hash    TEXT NOT NULL,
    gate_run_id      TEXT NOT NULL REFERENCES script_gate_runs(id),
    dependencies     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_script_approvals_item_id ON script_approvals (script_item_id);
CREATE INDEX IF NOT EXISTS idx_script_approvals_version_id ON script_approvals (script_version_id);

CREATE TABLE IF NOT EXISTS product_script_plans (
    id              TEXT PRIMARY KEY,
    script_item_id  TEXT NOT NULL REFERENCES script_items(id) ON DELETE CASCADE,
    version         INT NOT NULL DEFAULT 1,
    product_id      TEXT NOT NULL,
    target_duration_s INT NOT NULL,
    segment_count   INT NOT NULL,
    fingerprint     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_script_plan_item_version UNIQUE (script_item_id, version)
);

CREATE INDEX IF NOT EXISTS idx_script_plans_item_id ON product_script_plans (script_item_id);

CREATE TABLE IF NOT EXISTS script_segments (
    id               TEXT PRIMARY KEY,
    script_item_id   TEXT NOT NULL REFERENCES script_items(id) ON DELETE CASCADE,
    plan_id          TEXT NOT NULL REFERENCES product_script_plans(id) ON DELETE CASCADE,
    segment_index    INT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    intent           TEXT NOT NULL DEFAULT '',
    target_duration_s INT NOT NULL DEFAULT 0,
    display_text     TEXT NOT NULL DEFAULT '',
    spoken_text      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'draft',
    version          INT NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_script_segment_plan_index_version
        UNIQUE (plan_id, segment_index, version)
);

CREATE INDEX IF NOT EXISTS idx_script_segments_item_id ON script_segments (script_item_id);

CREATE TABLE IF NOT EXISTS script_generation_batches (
    id                    TEXT PRIMARY KEY,
    script_set_id         TEXT NOT NULL REFERENCES script_sets(id) ON DELETE CASCADE,
    status                TEXT NOT NULL DEFAULT 'queued',
    product_ids           JSONB NOT NULL DEFAULT '[]'::jsonb,
    job_ids               JSONB NOT NULL DEFAULT '[]'::jsonb,
    estimated_semantic_calls INT NOT NULL DEFAULT 0,
    idempotency_key       TEXT NOT NULL DEFAULT '',
    revision              INT NOT NULL DEFAULT 0,
    state                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    lease_owner           TEXT,
    lease_expires_at      TIMESTAMPTZ,
    lease_epoch           BIGINT NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE script_generation_batches
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_script_batches_set_id ON script_generation_batches (script_set_id);
CREATE INDEX IF NOT EXISTS idx_script_batches_idempotency_key ON script_generation_batches (idempotency_key);

CREATE TABLE IF NOT EXISTS script_generation_jobs (
    id                  TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL REFERENCES script_generation_batches(id) ON DELETE CASCADE,
    script_item_id      TEXT NOT NULL REFERENCES script_items(id),
    product_id          TEXT NOT NULL,
    intent              TEXT NOT NULL DEFAULT 'generate_long_form',
    status              TEXT NOT NULL DEFAULT 'queued',
    plan_id             TEXT,
    plan_segment_count  INT,
    current_segment_index INT NOT NULL DEFAULT 0,
    attempt_count       INT NOT NULL DEFAULT 0,
    target_duration_s   INT NOT NULL,
    fingerprint         JSONB,
    idempotency_key     TEXT NOT NULL DEFAULT '',
    lease_owner         TEXT,
    lease_expires_at    TIMESTAMPTZ,
    lease_epoch         BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE script_generation_jobs
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_script_jobs_batch_id ON script_generation_jobs (batch_id);
CREATE INDEX IF NOT EXISTS idx_script_jobs_item_id ON script_generation_jobs (script_item_id);

-- Batch idempotency fingerprint -> batch_id (survives restart; first wins).
CREATE TABLE IF NOT EXISTS script_idempotency (
    fingerprint TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotency: one live job per (item, intent, key) and one batch per (set, key)
-- only when a non-empty client key is supplied (empty keys are exempt).
CREATE UNIQUE INDEX IF NOT EXISTS ux_script_job_idem
    ON script_generation_jobs (script_item_id, intent, idempotency_key)
    WHERE idempotency_key <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ux_script_batch_idem
    ON script_generation_batches (script_set_id, idempotency_key)
    WHERE idempotency_key <> '';

-- Circular pointer FKs (created after all tables exist; idempotent via DO).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_item_current_version') THEN
        ALTER TABLE script_items ADD CONSTRAINT fk_item_current_version
            FOREIGN KEY (current_version_id) REFERENCES script_versions(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_item_approved_version') THEN
        ALTER TABLE script_items ADD CONSTRAINT fk_item_approved_version
            FOREIGN KEY (approved_version_id) REFERENCES script_versions(id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_version_gate_run') THEN
        ALTER TABLE script_versions ADD CONSTRAINT fk_version_gate_run
            FOREIGN KEY (gate_run_id) REFERENCES script_gate_runs(id);
    END IF;
END $$;
