from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kol_search.database import DatabaseConnection, DatabaseRuntime, DatabaseTarget
from .schema import MIGRATIONS


class AutomationDatabase:
    """SQLite connection and migration owner for the shared automation tables."""

    def __init__(self, path: DatabaseTarget, *, migrate: bool = True) -> None:
        self.runtime = DatabaseRuntime(path)
        self.path = self.runtime.path
        self.is_postgres = self.runtime.is_postgres
        if migrate and not self.is_postgres:
            self.migrate()

    def connect(self) -> DatabaseConnection:
        return self.runtime.connect()

    def wait_ready(self, *, timeout: float = 15) -> None:
        self.runtime.wait_ready(timeout=timeout)

    def close(self) -> None:
        self.runtime.close()

    @contextmanager
    def connection(self) -> Iterator[DatabaseConnection]:
        """Open a read-oriented connection, closing it after use."""
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[DatabaseConnection]:
        """Run all enclosed writes atomically, optionally acquiring the writer lock eagerly."""
        connection = self.connect()
        try:
            if not self.is_postgres:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        if self.is_postgres:
            raise RuntimeError("PostgreSQL schema migrations must be applied before runtime startup")
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM automation_schema_migrations"
                ).fetchall()
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                for statement in _sql_statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO automation_schema_migrations(version, name, applied_at)
                    VALUES(?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    (migration.version, migration.name),
                )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def applied_migrations(self) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT version, name, applied_at FROM automation_schema_migrations ORDER BY version"
            ).fetchall()
        return [dict(row) for row in rows]


def _sql_statements(script: str) -> Iterator[str]:
    """Split a migration without relying on executescript's implicit commits."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        import sqlite3

        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                yield statement
            pending = ""
    if pending.strip():
        raise ValueError("Incomplete SQL statement in automation migration")
