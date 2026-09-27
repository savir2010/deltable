from __future__ import annotations

import os

from .base import Store
from .sqlite import SqliteStore


def store_from_env() -> Store:
    """DELTA_MEMORY_DSN=postgres://... -> GBrain's Postgres; else DELTA_MEMORY_DB (SQLite file)."""
    dsn = os.environ.get("DELTA_MEMORY_DSN")
    if dsn:
        from .postgres import PostgresStore
        return PostgresStore(dsn)
    return SqliteStore(os.environ.get("DELTA_MEMORY_DB", "delta_memory.db"))


__all__ = ["Store", "SqliteStore", "store_from_env"]
