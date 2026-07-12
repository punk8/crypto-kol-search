from __future__ import annotations

import typer

from kol_search.db import Store
from kol_search.jobs import JobWorker
from kol_search.settings import get_settings


app = typer.Typer(help="Crypto KOL discovery and public-contact research")


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
    query: str = typer.Argument(..., help="Chinese or English crypto topic"),
    backend: str | None = typer.Option(None),
    limit: int = typer.Option(100, min=1, max=100),
    use_ai: bool = typer.Option(False),
) -> None:
    """Queue one discovery run; the command waits until it finishes."""
    import time

    settings = get_settings()
    selected = backend or settings.twitter_backend
    ready, reason = settings.backend_ready(selected)
    if not ready:
        raise typer.BadParameter(reason or "Backend is not configured")
    store = Store(settings.db_path())
    worker = JobWorker(store, settings)
    run_id = store.create_run(
        query=query,
        backend=selected,
        result_limit=limit,
        use_ai=use_ai,
        model=settings.openai_model,
    )
    worker.start()
    worker.notify()
    try:
        while True:
            run = store.get_run(run_id) or {}
            typer.echo(f"\r{run.get('progress', 0):3}% {run.get('phase', '')}", nl=False)
            if run.get("status") in {"completed", "completed_with_warnings", "failed", "interrupted"}:
                typer.echo()
                if run.get("error"):
                    typer.echo(run["error"], err=True)
                    raise typer.Exit(1)
                typer.echo(f"Run {run_id}: {run.get('candidate_count', 0)} candidates")
                return
            time.sleep(0.5)
    finally:
        worker.stop()

