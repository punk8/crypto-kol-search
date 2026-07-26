from __future__ import annotations

import sqlite3
from atexit import register
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
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
    def __init__(self, connection: Any, *, release: Any | None = None) -> None:
        self._connection = connection
        self._release = release
        self._closed = False

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
        if self._closed:
            return
        self._closed = True
        if self._release is None:
            self._connection.close()
            return
        try:
            if self.in_transaction:
                self._connection.rollback()
        finally:
            self._release(self._connection)


class DatabaseRuntime:
    """Open short-lived SQLite or PostgreSQL connections behind one boundary."""

    def __init__(self, target: DatabaseTarget) -> None:
        self.target = target
        self.is_postgres = is_postgres_target(target)
        self._pool: Any | None = None
        self._pool_lock = Lock()
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

        pool = self._postgres_pool()
        connection = pool.getconn(timeout=15)
        return PostgresConnection(connection, release=pool.putconn)

    def _postgres_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:  # pragma: no cover - packaging check
                raise RuntimeError(
                    'PostgreSQL pooling requires: pip install "psycopg[pool]"'
                ) from exc
            self._pool = ConnectionPool(
                conninfo=str(self.target),
                min_size=1,
                max_size=4,
                timeout=15,
                max_idle=300,
                max_lifetime=1800,
                open=True,
                kwargs={
                    "row_factory": dict_row,
                    "autocommit": False,
                    "prepare_threshold": None,
                    "options": "-c search_path=kol_search,public",
                },
            )
            register(self.close)
            return self._pool

    def wait_ready(self, *, timeout: float = 15) -> None:
        if self.is_postgres:
            self._postgres_pool().wait(timeout=timeout)

    def close(self) -> None:
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

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
