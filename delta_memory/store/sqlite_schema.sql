-- Local/demo backend. Same shape as schema/postgres.sql.
CREATE TABLE IF NOT EXISTS slots (
    entity_id    TEXT    NOT NULL,
    predicate    TEXT    NOT NULL,
    value_type   TEXT    NOT NULL,
    value_json   TEXT    NOT NULL,
    cardinality  TEXT    NOT NULL DEFAULT 'single',
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    confidence   REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (entity_id, predicate)
);

CREATE TABLE IF NOT EXISTS corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT    NOT NULL,
    predicate       TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    old_value_json  TEXT,
    new_value_json  TEXT    NOT NULL,
    error_magnitude REAL    NOT NULL,
    delta           REAL,
    version_from    INTEGER NOT NULL,
    version_to      INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    reason          TEXT    NOT NULL,
    ts              TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS corrections_key ON corrections (entity_id, predicate, id);

CREATE TABLE IF NOT EXISTS pending_reconciliation (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id           TEXT    NOT NULL,
    predicate           TEXT    NOT NULL,
    proposed_value_json TEXT    NOT NULL,
    current_value_json  TEXT,             -- the value that won; both candidates on one row
    read_version        INTEGER NOT NULL,
    current_version     INTEGER NOT NULL,
    source              TEXT    NOT NULL,
    confidence          REAL    NOT NULL,
    reason              TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'open',
    created_at          TEXT    NOT NULL,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS pending_key ON pending_reconciliation (entity_id, predicate, status);

CREATE TABLE IF NOT EXISTS key_aliases (
    kind   TEXT NOT NULL,   -- 'entity' | 'predicate'
    scope  TEXT NOT NULL,   -- '' for entity, entity_id for predicate
    alias  TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (kind, scope, alias)
);
