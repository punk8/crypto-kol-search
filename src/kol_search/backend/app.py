from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from kol_search.backend.connectors import build_connector_registry
from kol_search.backend.discover_api import router as discover_router
from kol_search.database import DatabaseRuntime
from kol_search.settings import get_settings


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    if settings.backend_host not in {"127.0.0.1", "localhost", "::1"}:
        if not settings.backend_api_token:
            raise RuntimeError(
                "KOL_BACKEND_API_TOKEN is required for a non-loopback backend bind."
            )
        if not settings.database_url:
            raise RuntimeError(
                "KOL_DATABASE_URL must point to PostgreSQL for a non-loopback backend."
            )

    database = DatabaseRuntime(settings.database_target())
    database.wait_ready()
    registry = build_connector_registry(settings)
    application.state.settings = settings
    application.state.database = database
    application.state.connector_registry = registry
    try:
        yield
    finally:
        registry.close()
        database.close()


app = FastAPI(
    title="Trend & KOL Discover Backend",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(discover_router)


@app.get("/health", tags=["health"])
def health(request: Request) -> dict[str, str]:
    return {
        "service": "trend-kol-discover-backend",
        "status": "ok",
        "version": "1.0.0",
        "database": (
            "postgresql" if request.app.state.database.is_postgres else "sqlite"
        ),
    }


__all__ = ["app"]
