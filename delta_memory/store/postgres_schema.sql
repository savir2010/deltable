-- Delta Memory tables, living inside GBrain's existing Postgres.
-- Own schema so nothing collides with GBrain's pages/chunks tables; no new infra.
-- Apply:  psql "$GBRAIN_DATABASE_URL" -f schema/postgres.sql
CREATE SCHEMA IF NOT EXISTS delta_memory;
SET search_path TO delta_memory;

CREATE TABLE IF NOT EXISTS slots (
    entity_id    TEXT        NOT NULL,
    predicate    TEXT        NOT NULL,
    value_type   TEXT        NOT NULL CHECK (value_type IN
                   ('int','float','date','path','id','enum','string','text')),
    value_json   JSONB       NOT NULL,          -- {"type": ..., "data": ...}
    cardinality  TEXT        NOT NULL DEFAULT 'single' CHECK (cardinality IN ('single','multi')),
    version      INTEGER     NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT        NOT NULL,          -- tool_call:git_diff | llm_extract | manual | failure_triggered
    confidence   REAL        NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY (entity_id, predicate)
);
CREATE INDEX IF NOT EXISTS slots_value_idx ON slots USING gin (value_json jsonb_path_ops);

-- Append-only audit log. Nothing is destroyed, only superseded.
CREATE TABLE IF NOT EXISTS corrections (
    id              BIGSERIAL   PRIMARY KEY,
    entity_id       TEXT        NOT NULL,
    predicate       TEXT        NOT NULL,
    kind            TEXT        NOT NULL CHECK (kind IN ('insert','correction','append','reconcile')),
    old_value_json  JSONB,
    new_value_json  JSONB       NOT NULL,
    error_magnitude REAL        NOT NULL,
    delta           DOUBLE PRECISION,
    version_from    INTEGER     NOT NULL,
    version_to      INTEGER     NOT NULL,
    source          TEXT        NOT NULL,
    reason          TEXT        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS corrections_key ON corrections (entity_id, predicate, id);

-- Enforce append-only at the database level.
CREATE OR REPLACE FUNCTION corrections_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'delta_memory.corrections is append-only';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS corrections_no_mutation ON corrections;
CREATE TRIGGER corrections_no_mutation BEFORE UPDATE OR DELETE ON corrections
    FOR EACH ROW EXECUTE FUNCTION corrections_append_only();

-- Writes that lost a version race (or were outranked on confidence) land here.
CREATE TABLE IF NOT EXISTS pending_reconciliation (
    id                  BIGSERIAL   PRIMARY KEY,
    entity_id           TEXT        NOT NULL,
    predicate           TEXT        NOT NULL,
    proposed_value_json JSONB       NOT NULL,
    current_value_json  JSONB,                     -- the value that won; both candidates on one row
    read_version        INTEGER     NOT NULL,
    current_version     INTEGER     NOT NULL,
    source              TEXT        NOT NULL,
    confidence          REAL        NOT NULL,
    reason              TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open','accepted','rejected','superseded')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ
);
ALTER TABLE pending_reconciliation ADD COLUMN IF NOT EXISTS current_value_json JSONB;
CREATE INDEX IF NOT EXISTS pending_key ON pending_reconciliation (entity_id, predicate, status);

-- Learned key resolutions: once "refund window" resolves to refund_policy.window_days
-- by similarity, later writes take the exact-match path.
CREATE TABLE IF NOT EXISTS key_aliases (
    kind   TEXT NOT NULL CHECK (kind IN ('entity','predicate')),
    scope  TEXT NOT NULL,
    alias  TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (kind, scope, alias)
);
