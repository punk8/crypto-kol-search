"""Standalone backend ASGI entrypoint for the user-managed server."""

from kol_search.backend.app import app

__all__ = ["app"]
