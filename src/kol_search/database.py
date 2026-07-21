from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


DatabaseTarget = str | Path


def is_postgres_target(target: DatabaseTarget) -> bool:
    value = str(target)
    return value.startswith("postgres://") or value.startswith("postgresql://")


def _postgres_sql(sql: str) -> str:
    """Translate DB-API qmark placeholders without touching quoted strings."""

    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


@runtime_checkable
class DatabaseConnection(Protocol):
    def execute(self, sql: str, parameters: Any = ()) -> Any: ...
    def executemany(self, sql: str, parameters: Any) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self._connection.info.transaction_status != TransactionStatus.IDLE

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        return self._connection.execute(_postgres_sql(sql), parameters)

    def executemany(self, sql: str, parameters: Any) -> Any:
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(sql), parameters)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class DatabaseRuntime:
    """Open short-lived SQLite or PostgreSQL connections behind one boundary."""

    def __init__(self, target: DatabaseTarget) -> None:
        self.target = target
        self.is_postgres = is_postgres_target(target)
        if not self.is_postgres:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path: Path | None = path
        else:
            self.path = None

    def connect(self) -> DatabaseConnection:
        if not self.is_postgres:
            connection = sqlite3.connect(
                str(self.path),
                timeout=10,
                check_same_thread=False,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection

        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised in packaging checks
            raise RuntimeError(
                "PostgreSQL requires psycopg; install the project runtime dependencies"
            ) from exc
        connection = psycopg.connect(
            str(self.target),
            row_factory=dict_row,
            autocommit=False,
            prepare_threshold=None,
            options="-c search_path=kol_search,public",
        )
        return PostgresConnection(connection)

    @contextmanager
    def connection(self) -> Iterator[DatabaseConnection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[DatabaseConnection]:
        connection = self.connect()
        try:
            if not self.is_postgres:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = [
    "DatabaseConnection",
    "DatabaseRuntime",
    "DatabaseTarget",
    "PostgresConnection",
    "is_postgres_target",
]
