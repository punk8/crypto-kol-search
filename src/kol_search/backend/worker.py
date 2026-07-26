from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from kol_search.backend.connectors import build_connector_registry
from kol_search.backend.connectors.x import XConnector
from kol_search.backend.discover_automation import DiscoverAutomationService
from kol_search.settings import Settings


logger = logging.getLogger(__name__)


class DiscoverWorker:
    """Supervised scheduler for autonomous discovery and Hot Content refreshes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = build_connector_registry(settings)
        connector = self.registry.get("x")
        if not isinstance(connector, XConnector):
            raise RuntimeError("The Discover worker requires the X connector.")
        self.service = DiscoverAutomationService(settings, connector)

    def close(self) -> None:
        self.service.close()
        self.registry.close()

    def refresh_domains(self) -> dict[str, Any]:
        try:
            result = self.service.refresh_domains()
            logger.info("domain refresh completed: %s", result)
            return result
        except Exception:
            logger.exception("domain refresh failed")
            raise

    def refresh_hot_content(self) -> dict[str, Any]:
        try:
            result = self.service.refresh_hot_content()
            logger.info("Hot Content refresh completed: %s", result)
            return result
        except Exception:
            logger.exception("Hot Content refresh failed")
            raise

    def run_once(self) -> dict[str, Any]:
        return {
            "domains": self.refresh_domains(),
            "hot_content": self.refresh_hot_content(),
        }

    def run_forever(self) -> None:
        scheduler = BlockingScheduler(timezone=self.settings.timezone)
        now = datetime.now(timezone.utc)
        scheduler.add_job(
            self.refresh_domains,
            "interval",
            hours=max(1, self.settings.discovery_interval_hours),
            id="x-multi-domain-discovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=now,
        )
        scheduler.add_job(
            self.refresh_hot_content,
            "interval",
            minutes=max(5, self.settings.hot_content_interval_minutes),
            id="x-hot-content-refresh",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=now,
        )
        try:
            scheduler.start()
        finally:
            self.close()


__all__ = ["DiscoverWorker"]
