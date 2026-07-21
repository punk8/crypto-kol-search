from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import Iterator

from kol_search.database import DatabaseConnection, DatabaseRuntime, DatabaseTarget


class NativePlatformDatabase:
    """Small SQLite boundary shared by platform-owned repositories."""

    def __init__(self, path: DatabaseTarget) -> None:
        self.runtime = DatabaseRuntime(path)
        self.path = self.runtime.path
        self.is_postgres = self.runtime.is_postgres
        self._state = local()

    def connect(self) -> DatabaseConnection:
        return self.runtime.connect()

    @contextmanager
    def transaction(self) -> Iterator[DatabaseConnection]:
        current = getattr(self._state, "connection", None)
        if current is not None:
            yield current
            return
        connection = self.connect()
        self._state.connection = connection
        try:
            if not self.is_postgres:
                connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._state.connection = None
            connection.close()
