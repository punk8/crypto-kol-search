from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kol_search.automation.database import AutomationDatabase
from kol_search.platforms import (
    PlatformRegistry,
    build_default_registry,
    install_registered_platform_schemas,
)
from kol_search.settings import Settings


@dataclass(frozen=True)
class RebuildResult:
    database_path: Path
    backup_path: Path | None
    platform_schemas: tuple[str, ...]


def rebuild_database(
    path: Path,
    *,
    backup: bool = True,
    settings: Settings | None = None,
    registry: PlatformRegistry | None = None,
) -> RebuildResult:
    """Explicitly replace one database file, preserving a timestamped backup by default."""

    target = path.expanduser().resolve()
    if target.exists() and not target.is_file():
        raise ValueError(f"Database target is not a regular file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path: Path | None = None

    if target.exists():
        if backup:
            backup_path = target.with_name(f"{target.name}.backup-{timestamp}")
            counter = 1
            while backup_path.exists():
                backup_path = target.with_name(
                    f"{target.name}.backup-{timestamp}-{counter}"
                )
                counter += 1
            target.replace(backup_path)
        else:
            target.unlink()

    # WAL sidecars cannot be reused by the new schema. Preserve them next to the
    # main backup when requested, otherwise remove only these exact files.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if not sidecar.exists():
            continue
        if backup_path:
            sidecar.replace(Path(f"{backup_path}{suffix}"))
        else:
            sidecar.unlink()

    owned_registry = registry is None
    active_registry = registry
    try:
        if active_registry is None:
            active_registry = build_default_registry(
                settings
                or Settings(
                    _env_file=None,
                    KOL_DB_PATH=str(target),
                    KOL_ENABLED_PLATFORMS="x,xiaohongshu",
                )
            )
        AutomationDatabase(target)
        installed = install_registered_platform_schemas(target, active_registry)
    except Exception:
        # A failed initialization must not strand the user without their old DB.
        failed: Path | None = None
        if target.exists():
            failed = target.with_name(f"{target.name}.failed-{timestamp}")
            target.replace(failed)
        if failed is not None:
            for suffix in ("-wal", "-shm"):
                failed_sidecar = Path(f"{failed}{suffix}")
                target_sidecar = Path(f"{target}{suffix}")
                if target_sidecar.exists():
                    target_sidecar.replace(failed_sidecar)
        if backup_path and backup_path.exists():
            backup_path.replace(target)
            for suffix in ("-wal", "-shm"):
                backup_sidecar = Path(f"{backup_path}{suffix}")
                if backup_sidecar.exists():
                    backup_sidecar.replace(Path(f"{target}{suffix}"))
        raise
    finally:
        if owned_registry and active_registry is not None:
            active_registry.close()
    return RebuildResult(
        database_path=target,
        backup_path=backup_path,
        platform_schemas=installed,
    )
