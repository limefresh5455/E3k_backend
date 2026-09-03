import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.config import (
    API_TITLE,
    API_VERSION,
    EMAIL_AUTOMATION_ENABLED,
    EMAIL_AUTOMATION_SECOND_SCHEDULE_HOUR,
    EMAIL_AUTOMATION_SECOND_SCHEDULE_MINUTE,
    EMAIL_AUTOMATION_SCHEDULE_HOUR,
    EMAIL_AUTOMATION_SCHEDULE_MINUTE,
)
from app.db import init_db
from app.services.erp_sync_job_service import trigger_sync
from app.supplier_addresses.database import init_supplier_address_db

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

logger = logging.getLogger("main")

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
DE_TIMEZONE = "Europe/Berlin"
SCHEDULE_HOUR = 0
SCHEDULE_MINUTE = 0
SYNC_SCHEDULE_HOUR = 6
SYNC_SCHEDULE_MINUTE = 15


def _scheduled_sync_job():
    result = trigger_sync()
    if result.get("started"):
        logger.info("[SCHEDULER] Nightly ERP-WC sync started.")
    else:
        logger.warning(
            "[SCHEDULER] Nightly ERP-WC sync skipped (already running, started_at=%s).",
            result.get("started_at"),
        )


def _scheduled_sync_route_job():
    try:
        from app.api.sync import sync_pcloud

        result = asyncio.run(sync_pcloud())
        logger.info(
            "[SCHEDULER] Daily sync route executed. total_found=%s processed=%s success=%s failure=%s",
            result.get("total_found", 0),
            result.get("processed", 0),
            result.get("success", 0),
            result.get("failure", 0),
        )
    except Exception as exc:
        logger.exception("[SCHEDULER] Daily sync route failed: %s", exc)


def _scheduled_email_automation_job():
    try:
        from app.email_automation.runner import run_email_automation

        exit_code = run_email_automation()
        if exit_code == 0:
            logger.info("[SCHEDULER] Email automation completed successfully.")
        elif exit_code == 2:
            logger.warning("[SCHEDULER] Email automation skipped because it is already running.")
        else:
            logger.error("[SCHEDULER] Email automation completed with errors.")
    except Exception as exc:
        logger.exception("[SCHEDULER] Email automation failed: %s", exc)


def _add_email_automation_jobs(scheduler, timezone):
    schedules = (
        (
            "daily_email_automation",
            EMAIL_AUTOMATION_SCHEDULE_HOUR,
            EMAIL_AUTOMATION_SCHEDULE_MINUTE,
        ),
        (
            "daily_email_automation_second",
            EMAIL_AUTOMATION_SECOND_SCHEDULE_HOUR,
            EMAIL_AUTOMATION_SECOND_SCHEDULE_MINUTE,
        ),
    )
    for job_id, hour, minute in schedules:
        scheduler.add_job(
            _scheduled_email_automation_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_supplier_address_db()
    if EMAIL_AUTOMATION_ENABLED:
        from app.email_automation.database import init_email_automation_db

        init_email_automation_db()
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
        scheduler.add_job(
            _scheduled_sync_route_job,
            trigger=CronTrigger(
                hour=SYNC_SCHEDULE_HOUR,
                minute=SYNC_SCHEDULE_MINUTE,
                timezone=de,
            ),
            id="daily_sync_route",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
        )
        if EMAIL_AUTOMATION_ENABLED:
            _add_email_automation_jobs(scheduler, de)
        scheduler.start()

        next_run = scheduler.get_job("nightly_erp_wc_sync").next_run_time
        logger.info(
            "[SCHEDULER] Nightly ERP-WC sync scheduled at %02d:%02d (%s). Next run: %s",
            SCHEDULE_HOUR,
            SCHEDULE_MINUTE,
            DE_TIMEZONE,
            next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

        sync_next_run = scheduler.get_job("daily_sync_route").next_run_time
        logger.info(
            "[SCHEDULER] Daily sync route scheduled at %02d:%02d (%s, Paris time). Next run: %s",
            SYNC_SCHEDULE_HOUR,
            SYNC_SCHEDULE_MINUTE,
            DE_TIMEZONE,
            sync_next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

        if EMAIL_AUTOMATION_ENABLED:
            email_schedules = (
                (
                    "daily_email_automation",
                    EMAIL_AUTOMATION_SCHEDULE_HOUR,
                    EMAIL_AUTOMATION_SCHEDULE_MINUTE,
                ),
                (
                    "daily_email_automation_second",
                    EMAIL_AUTOMATION_SECOND_SCHEDULE_HOUR,
                    EMAIL_AUTOMATION_SECOND_SCHEDULE_MINUTE,
                ),
            )
            for job_id, hour, minute in email_schedules:
                email_next_run = scheduler.get_job(job_id).next_run_time
                logger.info(
                    "[SCHEDULER] Email automation job '%s' scheduled at %02d:%02d (%s). Next run: %s",
                    job_id,
                    hour,
                    minute,
                    DE_TIMEZONE,
                    email_next_run.strftime("%Y-%m-%d %H:%M:%S %Z"),
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
