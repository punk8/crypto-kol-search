from __future__ import annotations

import typer

from kol_search.automation import AutomationStore
from kol_search.automation.service import AutomationWorker, PlatformAutomationService
from kol_search.database_rebuild import rebuild_database
from kol_search.platforms import build_default_registry, install_registered_platform_schemas
from kol_search.settings import get_settings


app = typer.Typer(help="Multi-platform KOL discovery and governed growth automation")
db_app = typer.Typer(help="Database maintenance commands")
app.add_typer(db_app, name="db")


def _platform_runtime(settings):  # noqa: ANN001, ANN202
    automation = AutomationStore(settings.db_path())
    registry = build_default_registry(settings)
    install_registered_platform_schemas(settings.db_path(), registry)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    worker = AutomationWorker(automation, service)
    return automation, registry, service, worker


@app.command()
def web(
    host: str | None = typer.Option(None, help="Bind host; defaults to 127.0.0.1"),
    port: int | None = typer.Option(None, help="Bind port"),
    reload: bool = typer.Option(False, help="Development auto-reload"),
) -> None:
    """Start the local operations dashboard."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "kol_search.web:app",
        host=host or settings.web_host,
        port=port or settings.web_port,
        reload=reload,
    )


@app.command("discover")
def discover(
    query: str = typer.Argument(..., help="Chinese or English KOL topic"),
    backend: str | None = typer.Option(None, help="Override the X read backend"),
    limit: int = typer.Option(100, min=1, max=100),
    platform: list[str] | None = typer.Option(
        None,
        "--platform",
        "-p",
        help="Platform ID; repeat for multiple platforms; defaults to x",
    ),
) -> None:
    """Run platform-native discovery and wait for every selected platform."""
    import time

    settings = get_settings()
    if backend:
        settings = settings.model_copy(update={"twitter_backend": backend})
    selected_platforms = tuple(dict.fromkeys(platform or ["x"]))
    automation, registry, service, worker = _platform_runtime(settings)
    unknown = set(selected_platforms) - set(registry.platform_ids)
    if unknown:
        raise typer.BadParameter(f"Unknown platform: {', '.join(sorted(unknown))}")
    run_ids = {
        platform_id: service.enqueue_discovery(
            platform_id, query=query, limit=limit, manual=True
        )
        for platform_id in selected_platforms
    }
    worker.start()
    worker.notify()
    try:
        while True:
            rows = {
                platform_id: automation.get_job(run_id) or {}
                for platform_id, run_id in run_ids.items()
            }
            progress = " · ".join(
                f"{platform_id} {row.get('progress', 0)}% {row.get('phase', '')}"
                for platform_id, row in rows.items()
            )
            typer.echo(f"\r{progress}", nl=False)
            if all(row.get("status") in {"succeeded", "failed", "cancelled"} for row in rows.values()):
                typer.echo()
                failures = [
                    f"{platform_id}: {row.get('error')}"
                    for platform_id, row in rows.items()
                    if row.get("status") == "failed"
                ]
                for platform_id, row in rows.items():
                    typer.echo(f"{platform_id} job #{row['id']}: {row.get('result') or {}}")
                if failures:
                    typer.echo("; ".join(failures), err=True)
                    raise typer.Exit(1)
                return
            time.sleep(0.5)
    finally:
        worker.stop()
        registry.close()


@app.command("platforms")
def list_platforms() -> None:
    """List registered platforms, declared capabilities, and configured health."""

    settings = get_settings()
    automation, registry, service, _worker = _platform_runtime(settings)
    try:
        connections = {
            (row["platform_id"], row["connection_key"]): row
            for row in automation.list_platform_connections()
        }
        for manifest in registry.list_manifests():
            reader = connections.get((manifest.platform_id, "reader"), {})
            capabilities = ", ".join(sorted(item.value for item in manifest.capabilities))
            typer.echo(
                f"{manifest.platform_id}\t{reader.get('status', 'disconnected')}\t{capabilities}"
            )
    finally:
        registry.close()


@app.command("scan")
def scan_platform(
    platform: str = typer.Option(..., "--platform", "-p"),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Wait for the manual scan to finish (default) or only enqueue it",
    ),
) -> None:
    """Run one platform-native signal scan."""

    import time

    settings = get_settings()
    automation, registry, service, worker = _platform_runtime(settings)
    try:
        if platform not in registry:
            raise typer.BadParameter(f"Unknown platform: {platform}")
        job_id = service.enqueue_signal_refresh(platform, manual=True)
        if not wait:
            typer.echo(f"Queued {platform} signal job #{job_id}")
            return
        worker.start()
        worker.notify()
        while True:
            row = automation.get_job(job_id) or {}
            typer.echo(
                f"\r{platform} {row.get('progress', 0)}% {row.get('phase', '')}",
                nl=False,
            )
            if row.get("status") in {"succeeded", "failed", "cancelled"}:
                typer.echo()
                if row.get("status") != "succeeded":
                    typer.echo(str(row.get("error") or row.get("status")), err=True)
                    raise typer.Exit(1)
                typer.echo(f"{platform} scan #{job_id}: {row.get('result') or {}}")
                return
            time.sleep(0.5)
    finally:
        worker.stop()
        registry.close()


@db_app.command("rebuild")
def rebuild_db(
    backup: bool = typer.Option(
        True, "--backup/--no-backup", help="Preserve the current DB before rebuilding"
    ),
) -> None:
    """Rebuild the local schema; this command is explicit and recoverable by default."""

    settings = get_settings()
    result = rebuild_database(settings.db_path(), backup=backup, settings=settings)
    typer.echo(f"Rebuilt {result.database_path}")
    if result.backup_path:
        typer.echo(f"Backup: {result.backup_path}")
