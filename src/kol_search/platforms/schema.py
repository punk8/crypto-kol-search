from __future__ import annotations

import sqlite3
from pathlib import Path

from kol_search.platforms.kernel import PlatformRegistry


def install_registered_platform_schemas(
    path: str | Path, registry: PlatformRegistry
) -> tuple[str, ...]:
    """Install platform-owned schemas in explicit registry order."""

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        installed = registry.install_schemas(connection)
    return installed


__all__ = ["install_registered_platform_schemas"]
