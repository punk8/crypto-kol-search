from __future__ import annotations

import typer

from kol_search.automation import AutomationStore
from kol_search.automation.service import PlatformAutomationService
from kol_search.database_rebuild import rebuild_database
from kol_search.platforms import build_default_registry, install_registered_platform_schemas
from kol_search.settings import get_settings


app = typer.Typer(help="Multi-platform trend and KOL discovery")
db_app = typer.Typer(help="Database maintenance commands")
app.add_typer(db_app, name="db")


def _platform_runtime(settings):  # noqa: ANN001, ANN202
    database_target = settings.database_target()
    automation = AutomationStore(database_target)
    registry = build_default_registry(settings)
    install_registered_platform_schemas(database_target, registry)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    return automation, registry, service


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


@app.command()
def backend(
    host: str | None = typer.Option(None, help="Backend bind host; defaults to 127.0.0.1"),
    port: int | None = typer.Option(None, help="Backend bind port"),
    reload: bool = typer.Option(False, help="Development auto-reload"),
) -> None:
    """Start the standalone authenticated Discover API service."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "kol_search.backend.app:app",
        host=host or settings.backend_host,
        port=port or settings.backend_port,
        reload=reload,
    )


@app.command("backend-scheduler")
def backend_scheduler(
    once: bool = typer.Option(False, "--once", help="Run one bounded refresh and exit"),
) -> None:
    """Run the server-side multi-domain discovery and Hot Content scheduler."""

    from kol_search.backend.worker import DiscoverWorker

    scheduler = DiscoverWorker(get_settings())
    if once:
        try:
            typer.echo(scheduler.run_once())
        finally:
            scheduler.close()
        return
    scheduler.run_forever()


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
    settings = get_settings()
    if backend:
        settings = settings.model_copy(update={"twitter_backend": backend})
    selected_platforms = tuple(dict.fromkeys(platform or ["x"]))
    _automation, registry, service = _platform_runtime(settings)
    unknown = set(selected_platforms) - set(registry.platform_ids)
    if unknown:
        raise typer.BadParameter(f"Unknown platform: {', '.join(sorted(unknown))}")
    try:
        for platform_id in selected_platforms:
            job_id = service.enqueue_discovery(
                platform_id, query=query, limit=limit, manual=True
            )
            row = service.execute_job_now(job_id)
            typer.echo(f"{platform_id} job #{job_id}: {row.get('result') or {}}")
    finally:
        registry.close()


@app.command("platforms")
def list_platforms() -> None:
    """List registered platforms, declared capabilities, and configured health."""

    settings = get_settings()
    automation, registry, _service = _platform_runtime(settings)
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
) -> None:
    """Run one platform-native signal scan."""

    settings = get_settings()
    _automation, registry, service = _platform_runtime(settings)
    try:
        if platform not in registry:
            raise typer.BadParameter(f"Unknown platform: {platform}")
        job_id = service.enqueue_signal_refresh(platform, manual=True)
        row = service.execute_job_now(job_id)
        typer.echo(f"{platform} scan #{job_id}: {row.get('result') or {}}")
    finally:
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
