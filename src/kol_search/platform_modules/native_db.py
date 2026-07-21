from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import Iterator


class NativePlatformDatabase:
    """Small SQLite boundary shared by platform-owned repositories."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._state = local()
        path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        current = getattr(self._state, "connection", None)
        if current is not None:
            yield current
            return
        connection = self.connect()
        self._state.connection = connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._state.connection = None
            connection.close()
