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
