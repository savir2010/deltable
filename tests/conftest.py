"""Every test runs on SQLite; set DELTA_MEMORY_TEST_DSN to also run it on Postgres (GBrain's DB)."""
import os

import pytest

from delta_memory import DeltaMemory, SqliteStore

BACKENDS = ["sqlite", "sqlite_single_conn"] + (["postgres"] if os.environ.get("DELTA_MEMORY_TEST_DSN") else [])


@pytest.fixture(params=BACKENDS)
def dm(request, tmp_path):
    if request.param == "sqlite":
        return DeltaMemory(SqliteStore(str(tmp_path / "t.db")))
    if request.param == "sqlite_single_conn":  # the networked server's mode
        return DeltaMemory(SqliteStore(str(tmp_path / "t.db"), single_connection=True))
    import psycopg

    from delta_memory.store.postgres import PostgresStore
    dsn = os.environ["DELTA_MEMORY_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS delta_memory CASCADE")
    return DeltaMemory(PostgresStore(dsn))
