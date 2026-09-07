"""Small PostgreSQL helpers and explicit SQL migration runner."""
from __future__ import annotations

from importlib import resources

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str):
        self.pool = ConnectionPool(url, kwargs={"row_factory": dict_row}, open=False)

    def open(self):
        self.pool.open(wait=True)

    def close(self):
        self.pool.close()

    def connection(self):
        return self.pool.connection()


def migrate(db: Database) -> None:
    scripts = sorted(resources.files("elengtis.migrations").glob("*.sql"), key=lambda path: path.name)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(82491832)")
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in cur.fetchall()}
        for script in scripts:
            if script.name not in applied:
                cur.execute(script.read_text())
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (script.name,))
