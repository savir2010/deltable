"""Postgres backend: the delta_memory schema inside GBrain's database.

    pip install 'delta-memory[postgres]'
    psql "$GBRAIN_DATABASE_URL" -f schema/postgres.sql
    DELTA_MEMORY_DSN=$GBRAIN_DATABASE_URL delta-memory-mcp
"""
from __future__ import annotations

from contextlib import contextmanager
from importlib import resources

from ..types import Correction, Pending, Slot, TypedValue
from .base import Store
from .rows import (correction_from_row, correction_params, dump_value, pending_from_row,
                   slot_from_row)

_INSERT_CORRECTION = """
INSERT INTO corrections (entity_id, predicate, kind, old_value_json, new_value_json,
    error_magnitude, delta, version_from, version_to, source, reason, ts)
VALUES (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
"""



class PostgresStore(Store):
    def __init__(self, dsn: str, apply_schema: bool = True):
        import psycopg
        from psycopg.rows import dict_row

        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        if apply_schema:
            ddl = resources.files(__package__).joinpath("postgres_schema.sql").read_text()
            with self._psycopg.connect(dsn, autocommit=True) as c:
                c.execute(ddl)

    @contextmanager
    def _conn(self):
        with self._psycopg.connect(self.dsn, row_factory=self._dict_row,
                                   options="-c search_path=delta_memory") as c:
            yield c  # psycopg commits on clean exit, rolls back on exception

    def _one(self, sql, args=()):
        with self._conn() as c:
            return c.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self._conn() as c:
            return c.execute(sql, args).fetchall()

    # --- reads -------------------------------------------------------------
    def get_slot(self, entity_id, predicate):
        r = self._one("SELECT * FROM slots WHERE entity_id=%s AND predicate=%s", (entity_id, predicate))
        return slot_from_row(r) if r else None

    def list_entities(self):
        return [r["entity_id"] for r in self._all("SELECT DISTINCT entity_id FROM slots ORDER BY 1")]

    def list_predicates(self, entity_id):
        return [r["predicate"] for r in self._all(
            "SELECT predicate FROM slots WHERE entity_id=%s ORDER BY 1", (entity_id,))]

    def find_by_value(self, value: TypedValue, predicate=None):
        q, args = "SELECT * FROM slots WHERE value_json = %s::jsonb", [dump_value(value)]
        if predicate:
            q += " AND predicate=%s"
            args.append(predicate)
        return [slot_from_row(r) for r in self._all(q, args)]

    def get_aliases(self, kind, scope):
        return {r["alias"]: r["target"] for r in self._all(
            "SELECT alias, target FROM key_aliases WHERE kind=%s AND scope=%s", (kind, scope))}

    def history(self, entity_id, predicate):
        return [correction_from_row(r) for r in self._all(
            "SELECT * FROM corrections WHERE entity_id=%s AND predicate=%s ORDER BY id",
            (entity_id, predicate))]

    def recent_corrections(self, limit=50):
        return [correction_from_row(r) for r in self._all(
            "SELECT * FROM corrections ORDER BY id DESC LIMIT %s", (limit,))]

    def list_pending(self, entity_id=None, predicate=None, status="open"):
        q, args = "SELECT * FROM pending_reconciliation WHERE true", []
        for col, v in (("entity_id", entity_id), ("predicate", predicate), ("status", status)):
            if v is not None:
                q += f" AND {col}=%s"
                args.append(v)
        return [pending_from_row(r) for r in self._all(q + " ORDER BY id", args)]

    def get_pending(self, pending_id):
        r = self._one("SELECT * FROM pending_reconciliation WHERE id=%s", (pending_id,))
        return pending_from_row(r) if r else None

    # --- writes ------------------------------------------------------------
    def add_alias(self, kind, scope, alias, target):
        with self._conn() as c:
            c.execute("""INSERT INTO key_aliases VALUES (%s,%s,%s,%s)
                         ON CONFLICT (kind, scope, alias) DO UPDATE SET target=EXCLUDED.target""",
                      (kind, scope, alias, target))

    def insert_slot(self, slot: Slot, log: Correction) -> bool:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO slots VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
                   ON CONFLICT (entity_id, predicate) DO NOTHING""",
                (slot.entity_id, slot.predicate, slot.value.type.value, dump_value(slot.value),
                 slot.cardinality.value, slot.version, slot.updated_at, slot.source, slot.confidence))
            if cur.rowcount == 0:
                return False
            c.execute(_INSERT_CORRECTION, correction_params(log))
            return True

    def cas_update(self, entity_id, predicate, expected_version, new_value, source, confidence, log):
        with self._conn() as c:
            r = c.execute(
                """UPDATE slots SET value_type=%s, value_json=%s::jsonb, version=version+1,
                       updated_at=now(), source=%s, confidence=%s
                   WHERE entity_id=%s AND predicate=%s AND version=%s
                   RETURNING *""",
                (new_value.type.value, dump_value(new_value), source, confidence,
                 entity_id, predicate, expected_version)).fetchone()
            if r is None:
                return None
            c.execute(_INSERT_CORRECTION, correction_params(log))
            return slot_from_row(r)

    def add_pending(self, p: Pending) -> int:
        r = None
        with self._conn() as c:
            r = c.execute(
                """INSERT INTO pending_reconciliation (entity_id, predicate, proposed_value_json,
                       current_value_json, read_version, current_version, source, confidence,
                       reason, status, created_at)
                   VALUES (%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (p.entity_id, p.predicate, dump_value(p.proposed_value),
                 dump_value(p.current_value), p.read_version,
                 p.current_version, p.source, p.confidence, p.reason, p.status, p.created_at)).fetchone()
        return r["id"]

    def set_pending_status(self, pending_id, status):
        with self._conn() as c:
            c.execute("UPDATE pending_reconciliation SET status=%s, resolved_at=now() WHERE id=%s",
                      (status, pending_id))
