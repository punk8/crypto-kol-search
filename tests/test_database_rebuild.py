import sqlite3
from pathlib import Path

import pytest

from kol_search.database_rebuild import rebuild_database
from kol_search.platforms import PlatformManifest, PlatformPlugin, PlatformRegistry


def test_rebuild_database_preserves_backup_and_installs_all_schemas(tmp_path: Path) -> None:
    path = tmp_path / "kol.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE old_data(value TEXT)")
    connection.execute("INSERT INTO old_data VALUES('keep me')")
    connection.commit()
    connection.close()

    result = rebuild_database(path)
    assert result.backup_path and result.backup_path.exists()

    with sqlite3.connect(result.backup_path) as backup:
        assert backup.execute("SELECT value FROM old_data").fetchone()[0] == "keep me"
    with sqlite3.connect(path) as rebuilt:
        tables = {
            row[0]
            for row in rebuilt.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "automation_jobs",
        "automation_brand_config",
        "automation_account_quotas",
        "x_accounts",
        "xhs_users",
    } <= tables
    assert "runs" not in tables


def test_rebuild_restores_original_database_when_a_platform_schema_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restore.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old_data(value TEXT)")
        connection.execute("INSERT INTO old_data VALUES('recover me')")

    def broken_schema(_connection):  # noqa: ANN001, ANN202
        raise RuntimeError("broken platform migration")

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="broken", name="Broken", version="test"
                ),
                schema_installer=broken_schema,
            )
        ]
    )
    with pytest.raises(RuntimeError, match="broken platform migration"):
        rebuild_database(path, registry=registry)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM old_data").fetchone()[0] == "recover me"
    assert list(tmp_path.glob("restore.db.failed-*"))
