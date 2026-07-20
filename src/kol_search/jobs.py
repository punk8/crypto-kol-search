from __future__ import annotations

import logging
import threading
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from kol_search.db import Store
from kol_search.discovery.pipeline import DiscoveryPipeline
from kol_search.discovery.multiplatform import MultiPlatformDiscoveryPipeline
from kol_search.discovery.seed_pipeline import SeedPipeline
from kol_search.discovery.signals import SignalPipeline
from kol_search.settings import Settings


logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.pipeline = DiscoveryPipeline(store, settings)
        self.multiplatform_pipeline = MultiPlatformDiscoveryPipeline(store, settings)
        self.seed_pipeline = SeedPipeline(store, settings)
        self.signal_pipeline = SignalPipeline(store, settings)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.interrupt_running_jobs()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kol-job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def notify(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self.store.claim_next_job()
            if not job:
                self._wake.wait(1)
                self._wake.clear()
                continue
            run_id = int(job["run_id"])
            run = self.store.get_run(run_id)
            if not run:
                continue
            latest_stats: dict[str, Any] = {}
            latest_warnings: list[str] = []

            def progress(percent: int, phase: str, stats: dict, warnings: list[str]) -> None:
                nonlocal latest_stats, latest_warnings
                latest_stats = dict(stats)
                latest_warnings = list(dict.fromkeys(warnings))[-100:]
                self.store.update_run(
                    run_id,
                    progress=percent,
                    phase=phase,
                    stats=latest_stats,
                    warnings=latest_warnings,
                )

            try:
                pipeline = (
                    self.signal_pipeline
                    if run.get("kind") == "signal_scan"
                    else (
                        self.seed_pipeline
                        if run.get("kind") in {"seed_build", "seed_expand"}
                        else (
                            self.multiplatform_pipeline
                            if run.get("kind") == "multiplatform"
                            else self.pipeline
                        )
                    )
                )
                candidates = pipeline.run(run, progress)
                status = "completed_with_warnings" if latest_warnings else "completed"
                self.store.finish_run(
                    run_id,
                    len(candidates),
                    status=status,
                    stats=latest_stats,
                    warnings=latest_warnings,
                )
            except Exception as exc:  # worker boundary: persist every failure
                logger.exception("KOL discovery run %s failed", run_id)
                self.store.finish_run(
                    run_id,
                    0,
                    status="failed",
                    error=str(exc),
                    stats=latest_stats,
                    warnings=latest_warnings,
                )


class WeeklyScheduler:
    def __init__(self, store: Store, settings: Settings, worker: JobWorker) -> None:
        self.store = store
        self.settings = settings
        self.worker = worker
        self.scheduler = BackgroundScheduler(timezone=settings.timezone)

    def start(self) -> None:
        configured = False
        if self.settings.enable_weekly_refresh:
            self.scheduler.add_job(
                self.enqueue_global_refresh,
                trigger="cron",
                day_of_week=self.settings.weekly_day,
                hour=self.settings.weekly_hour,
                minute=0,
                id="weekly-global-refresh",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=86400,
                max_instances=1,
            )
            configured = True
        if self.settings.enable_signal_scan:
            self.scheduler.add_job(
                self.enqueue_signal_scan,
                trigger="interval",
                minutes=max(5, self.settings.signal_interval_minutes),
                id="signal-scan",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=1800,
                max_instances=1,
            )
            configured = True
        if configured:
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def enqueue_global_refresh(self) -> int | None:
        ready, reason = self.settings.backend_ready(self.settings.twitter_backend)
        if not ready:
            logger.warning("Weekly refresh skipped: %s", reason)
            return None
        run_id = self.store.create_run(
            query="global crypto library",
            backend=self.settings.twitter_backend,
            kind="global",
            language="all",
            account_type="all",
            min_followers=1000,
            result_limit=100,
            use_ai=bool(self.settings.openai_api_key),
            model=self.settings.openai_model,
            config={"scheduled": True, "timezone": self.settings.timezone},
        )
        self.worker.notify()
        return run_id

    def enqueue_signal_scan(self) -> int | None:
        if self.store.has_active_run("signal_scan"):
            return None
        ready, reason = self.settings.backend_ready(self.settings.twitter_backend)
        if not ready:
            logger.warning("Signal scan skipped: %s", reason)
            return None
        brand = self.store.get_brand_profile()
        if not brand.get("brand_name") or not brand.get("x_handle"):
            logger.warning("Signal scan skipped: brand profile is incomplete")
            return None
        run_id = self.store.create_run(
            query="X KOL and topic signal scan",
            backend=self.settings.twitter_backend,
            kind="signal_scan",
            language="all",
            account_type="person",
            min_followers=0,
            result_limit=self.settings.signal_reply_limit + self.settings.signal_topic_limit,
            use_ai=bool(self.settings.openai_api_key),
            model=self.settings.openai_model,
            config={
                "scheduled": True,
                "interval_minutes": self.settings.signal_interval_minutes,
            },
        )
        self.worker.notify()
        return run_id
