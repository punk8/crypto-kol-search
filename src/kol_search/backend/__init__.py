"""Request-scoped data acquisition and processing service."""

from kol_search.backend.router import router
from kol_search.backend.discover_api import router as discover_router

__all__ = ["discover_router", "router"]
