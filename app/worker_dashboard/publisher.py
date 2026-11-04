"""Single-process background runner for finalized SFTP uploads."""

from __future__ import annotations

import logging
from threading import Event, Thread

from .config import DashboardSettings
from .database import (
    DashboardPublicationAlreadyRunning,
    DashboardPublicationLock,
)
from .storage import DatasetStore


logger = logging.getLogger("worker_dashboard.publisher")


class DashboardPublisher:
    def __init__(self, settings: DashboardSettings):
        self.settings = settings
        self.store = DatasetStore(settings)
        self._stopped = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.settings.publisher_enabled or self._thread is not None:
            return
        self.store.prepare_directories()
        self._thread = Thread(
            target=self._run,
            name="dashboard-publisher",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Dashboard publisher started with a %d second interval",
            self.settings.publish_interval_seconds,
        )

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                with DashboardPublicationLock():
                    summary = self.store.scan_and_publish()
                if summary.published or summary.rejected:
                    logger.info(
                        "Dashboard scan complete: published=%d unchanged=%d rejected=%d",
                        summary.published,
                        summary.unchanged,
                        summary.rejected,
                    )
            except DashboardPublicationAlreadyRunning:
                logger.info("Dashboard publication skipped; another process is running")
            except Exception:
                logger.exception("Unexpected dashboard publication failure")
            self._stopped.wait(self.settings.publish_interval_seconds)
