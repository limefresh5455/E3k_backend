import logging
import threading
from datetime import datetime, timezone

from app.services.erp_wc_sync_service import run_sync

logger = logging.getLogger("erp_sync_job_service")

sync_state = {
    "is_running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error": None,
}
state_lock = threading.Lock()


def _run_sync_job():
    with state_lock:
        sync_state["is_running"] = True
        sync_state["started_at"] = datetime.now(timezone.utc).isoformat()
        sync_state["finished_at"] = None
        sync_state["last_result"] = None
        sync_state["last_error"] = None

    logger.info("[JOB] ERP-WC sync started at %s", sync_state["started_at"])
    try:
        result = run_sync()
        with state_lock:
            sync_state["last_result"] = result
            sync_state["last_error"] = None
        logger.info("[JOB] ERP-WC sync completed successfully.")
    except Exception as exc:
        logger.exception("[JOB] ERP-WC sync crashed: %s", exc)
        with state_lock:
            sync_state["last_error"] = str(exc)
    finally:
        with state_lock:
            sync_state["is_running"] = False
            sync_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("[JOB] ERP-WC sync finished at %s", sync_state["finished_at"])


def trigger_sync_in_thread() -> None:
    thread = threading.Thread(target=_run_sync_job, daemon=True, name="erp-wc-sync")
    thread.start()


def trigger_sync() -> dict:
    with state_lock:
        if sync_state["is_running"]:
            return {
                "started": False,
                "reason": "already_running",
                "started_at": sync_state["started_at"],
            }

    trigger_sync_in_thread()
    return {
        "started": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def get_sync_status() -> dict:
    with state_lock:
        return {
            "is_running": sync_state["is_running"],
            "started_at": sync_state["started_at"],
            "finished_at": sync_state["finished_at"],
            "last_result": sync_state["last_result"],
            "last_error": sync_state["last_error"],
        }
