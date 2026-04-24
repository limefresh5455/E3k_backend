import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.config import API_TITLE, API_VERSION
from app.db import init_db
from app.services.erp_sync_job_service import trigger_sync

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

logger = logging.getLogger("main")

ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
DE_TIMEZONE = "Europe/Berlin"
SCHEDULE_HOUR = 0
SCHEDULE_MINUTE = 0


def _scheduled_sync_job():
    result = trigger_sync()
    if result.get("started"):
        logger.info("[SCHEDULER] Nightly ERP-WC sync started.")
    else:
        logger.warning(
            "[SCHEDULER] Nightly ERP-WC sync skipped (already running, started_at=%s).",
            result.get("started_at"),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = None

    if ENABLE_SCHEDULER and not APSCHEDULER_AVAILABLE:
        logger.warning(
            "[SCHEDULER] ENABLE_SCHEDULER=true but APScheduler/pytz is not installed. "
            "Install with: pip install apscheduler pytz"
        )

    if ENABLE_SCHEDULER and APSCHEDULER_AVAILABLE:
        de = pytz.timezone(DE_TIMEZONE)
        scheduler = BackgroundScheduler(timezone=de)
        scheduler.add_job(
            _scheduled_sync_job,
            trigger=CronTrigger(
                hour=SCHEDULE_HOUR,
                minute=SCHEDULE_MINUTE,
                timezone=de,
            ),
            id="nightly_erp_wc_sync",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )
        scheduler.start()

        next_run = scheduler.get_job("nightly_erp_wc_sync").next_run_time
        logger.info(
            "[SCHEDULER] Nightly ERP-WC sync scheduled at %02d:%02d (%s). Next run: %s",
            SCHEDULE_HOUR,
            SCHEDULE_MINUTE,
            DE_TIMEZONE,
            next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
        logger.info("[SCHEDULER] Shutdown.")

app = FastAPI(title=API_TITLE, version=API_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
