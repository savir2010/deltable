"""SQLite backend: zero-infra stand-in for GBrain's Postgres (demo, tests, laptops).

Two connection modes:

* single_connection=True (the networked server, net_server.py): the server process owns
  ONE connection and every tool call from every client goes through it, under a lock.
  The server is the single source of truth; clients never open the DB file themselves.
* single_connection=False (default; tests, stdio server): a fresh connection per
  operation, so threads or separate processes can share one file.

On concurrency, precisely: WAL does NOT make writes parallel. SQLite always has at most
one writer. What WAL buys here is that readers (the demo's live inspector, `sqlite3` in
another terminal) can read a consistent snapshot while the server is writing, instead
of blocking on it. What decides a race between two agents is the version check in
cas_update: both send the same expected_version, their writes are applied one after the
other, and the second one's stale version fails `WHERE version = ?` (rowcount 0), so it
is routed to pending_reconciliation instead of overwriting.
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from importlib import resources

from ..types import Correction, Pending, Slot, TypedValue, now_iso
from .base import Store
from .rows import (correction_from_row, correction_params, dump_value, pending_from_row,
                   slot_from_row)

_INSERT_CORRECTION = """
INSERT INTO corrections (entity_id, predicate, kind, old_value_json, new_value_json,
    error_magnitude, delta, version_from, version_to, source, reason, ts)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
"""


class SqliteStore(Store):
    def __init__(self, path: str = "delta_memory.db", single_connection: bool = False):
        self.path = path
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if single_connection:
            self._shared = self._open()
        ddl = resources.files(__package__).joinpath("sqlite_schema.sql").read_text()
        # Switching journal mode can return SQLITE_BUSY without consulting busy_timeout
        # when several processes open a fresh DB at once, so retry init explicitly.
        for attempt in range(50):
            try:
                with self._conn() as c:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.executescript(ddl)
                    cols = {r[1] for r in c.execute("PRAGMA table_info(pending_reconciliation)")}
                    if "current_value_json" not in cols:  # migrate DBs created before this column
                        c.execute("ALTER TABLE pending_reconciliation ADD COLUMN current_value_json TEXT")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.05 + random.random() * 0.05)

    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        return c

    @contextmanager
    def _conn(self):
        if self._shared is not None:
            # FastMCP runs sync tools inline on one event loop, so calls already can't
            # interleave; the lock keeps that true if tools ever move to a thread pool.
            with self._lock:
                yield self._shared
            return
        c = self._open()
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _tx(self):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # --- reads -------------------------------------------------------------
    def get_slot(self, entity_id, predicate):
        with self._conn() as c:
            r = c.execute("SELECT * FROM slots WHERE entity_id=? AND predicate=?",
                          (entity_id, predicate)).fetchone()
        return slot_from_row(r) if r else None

    def list_entities(self):
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT entity_id FROM slots ORDER BY 1")]

    def list_predicates(self, entity_id):
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT predicate FROM slots WHERE entity_id=? ORDER BY 1", (entity_id,))]

    def find_by_value(self, value: TypedValue, predicate=None):
        q, args = "SELECT * FROM slots WHERE value_json=?", [dump_value(value)]
        if predicate:
            q += " AND predicate=?"
            args.append(predicate)
        with self._conn() as c:
            return [slot_from_row(r) for r in c.execute(q, args)]

    def get_aliases(self, kind, scope):
        with self._conn() as c:
            return {r["alias"]: r["target"] for r in c.execute(
                "SELECT alias, target FROM key_aliases WHERE kind=? AND scope=?", (kind, scope))}

    def history(self, entity_id, predicate):
        with self._conn() as c:
            return [correction_from_row(r) for r in c.execute(
                "SELECT * FROM corrections WHERE entity_id=? AND predicate=? ORDER BY id",
                (entity_id, predicate))]

    def recent_corrections(self, limit=50):
        with self._conn() as c:
            return [correction_from_row(r) for r in c.execute(
                "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (limit,))]

    def list_pending(self, entity_id=None, predicate=None, status="open"):
        q, args = "SELECT * FROM pending_reconciliation WHERE 1=1", []
        for col, v in (("entity_id", entity_id), ("predicate", predicate), ("status", status)):
            if v is not None:
                q += f" AND {col}=?"
                args.append(v)
        with self._conn() as c:
            return [pending_from_row(r) for r in c.execute(q + " ORDER BY id", args)]

    def get_pending(self, pending_id):
        with self._conn() as c:
            r = c.execute("SELECT * FROM pending_reconciliation WHERE id=?", (pending_id,)).fetchone()
        return pending_from_row(r) if r else None

    # --- writes ------------------------------------------------------------
    def add_alias(self, kind, scope, alias, target):
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO key_aliases VALUES (?,?,?,?)", (kind, scope, alias, target))

    def insert_slot(self, slot: Slot, log: Correction) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO slots VALUES (?,?,?,?,?,?,?,?,?)",
                (slot.entity_id, slot.predicate, slot.value.type.value, dump_value(slot.value),
                 slot.cardinality.value, slot.version, slot.updated_at, slot.source, slot.confidence))
            if cur.rowcount == 0:
                return False
            c.execute(_INSERT_CORRECTION, correction_params(log))
            return True

    def cas_update(self, entity_id, predicate, expected_version, new_value, source, confidence, log):
        ts = now_iso()
        with self._tx() as c:
            cur = c.execute(
                """UPDATE slots SET value_type=?, value_json=?, version=version+1,
                       updated_at=?, source=?, confidence=?
                   WHERE entity_id=? AND predicate=? AND version=?""",
                (new_value.type.value, dump_value(new_value), ts, source, confidence,
                 entity_id, predicate, expected_version))
            if cur.rowcount == 0:
                return None  # version moved underneath us: caller routes to reconciliation
            c.execute(_INSERT_CORRECTION, correction_params(log))
            r = c.execute("SELECT * FROM slots WHERE entity_id=? AND predicate=?",
                          (entity_id, predicate)).fetchone()
        return slot_from_row(r)

    def add_pending(self, p: Pending) -> int:
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO pending_reconciliation (entity_id, predicate, proposed_value_json,
                       current_value_json, read_version, current_version, source, confidence,
                       reason, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (p.entity_id, p.predicate, dump_value(p.proposed_value),
                 dump_value(p.current_value), p.read_version,
                 p.current_version, p.source, p.confidence, p.reason, p.status, p.created_at))
            return cur.lastrowid

    def set_pending_status(self, pending_id, status):
        with self._tx() as c:
            c.execute("UPDATE pending_reconciliation SET status=?, resolved_at=? WHERE id=?",
                      (status, now_iso(), pending_id))
