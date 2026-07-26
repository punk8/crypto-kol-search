from __future__ import annotations

from threading import RLock

from kol_search.backend.connectors.base import (
    ConnectorFactory,
    ConnectorNotFoundError,
    PlatformConnector,
)
from kol_search.backend.models import PlatformConnectorDescriptor


class ConnectorRegistry:
    """Lazy registry for independently replaceable platform connectors."""

    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}
        self._instances: dict[str, PlatformConnector] = {}
        self._lock = RLock()

    def register(self, platform_id: str, factory: ConnectorFactory) -> None:
        key = platform_id.strip().lower()
        if not key:
            raise ValueError("platform_id cannot be empty")
        with self._lock:
            if key in self._factories:
                raise ValueError(f"Connector already registered: {key}")
            self._factories[key] = factory

    def get(self, platform_id: str) -> PlatformConnector:
        key = platform_id.strip().lower()
        with self._lock:
            factory = self._factories.get(key)
            if factory is None:
                raise ConnectorNotFoundError(
                    f"Unsupported platform connector: {platform_id}"
                )
            connector = self._instances.get(key)
            if connector is None:
                connector = factory()
                self._instances[key] = connector
            return connector

    def descriptors(self) -> list[PlatformConnectorDescriptor]:
        return [self.get(key).descriptor() for key in sorted(self._factories)]

    def close(self) -> None:
        with self._lock:
            instances = tuple(self._instances.values())
            self._instances.clear()
        for connector in instances:
            connector.close()
