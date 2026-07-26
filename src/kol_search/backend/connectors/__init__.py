from __future__ import annotations

from kol_search.backend.connectors.registry import ConnectorRegistry
from kol_search.backend.connectors.x import XConnector
from kol_search.settings import Settings


def build_connector_registry(settings: Settings) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    if "x" in settings.platform_ids():
        registry.register("x", lambda: XConnector(settings))
    return registry


__all__ = ["ConnectorRegistry", "build_connector_registry"]
