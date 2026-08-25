"""One-shot entry point for the scheduled IMAP-to-pCloud job."""

from datetime import datetime

from app.config import (
    EMAIL_AUTOMATION_EXCEL_FILE,
    EMAIL_AUTOMATION_PATH_LOG_FILE,
    IMAP_AUTH_METHOD,
    IMAP_FOLDER,
    IMAP_HOST,
    IMAP_OAUTH2_TOKEN,
    IMAP_PASSWORD,
    IMAP_PORT,
    IMAP_TIMEOUT_SECONDS,
    IMAP_USERNAME,
    IMAP_USE_SSL,
    PCLOUD_ACCESS_TOKEN,
    PCLOUD_API_HOST,
    PCLOUD_ROOT_FOLDER_ID,
    PCLOUD_TIMEOUT_SECONDS,
)
from app.email_automation import config
from app.email_automation.database import (
    EmailAutomationLock,
    JobAlreadyRunning,
    init_email_automation_db,
)
from app.email_automation.email_processor import process_emails
from app.email_automation.imap_client import ImapClient
from app.email_automation.logger import setup_logging
from app.email_automation.path_logger import write_daily_path_log
from app.email_automation.pcloud_upload_client import PCloudClient, safe_cloud_name
from app.email_automation.repository import UploadHistory
from app.email_automation.supplier_mapping import load_supplier_mapping


def run_email_automation() -> int:
    logger = setup_logging()
    start_time = datetime.now()
    logger.info("IMAP to pCloud PDF automation starting")

    errors = config.validate_configuration()
    if errors:
        for error in errors:
            logger.error("Configuration: %s", error)
        return 1

    try:
        init_email_automation_db()
        with EmailAutomationLock():
            return _run_job(logger, start_time)
    except JobAlreadyRunning as exc:
        logger.warning("%s", exc)
        return 2
    except Exception as exc:
        logger.exception("Fatal email automation error: %s", exc)
        return 1


def _run_job(logger, start_time: datetime) -> int:
    supplier_map, domain_map = load_supplier_mapping(EMAIL_AUTOMATION_EXCEL_FILE, logger)
    if not supplier_map and not domain_map:
        logger.error("No supplier mappings loaded")
        return 1

    folder_names = {}
    for supplier_name in set(supplier_map.values()) | set(domain_map.values()):
        cloud_name = safe_cloud_name(supplier_name, "Unknown Supplier").casefold()
        previous = folder_names.get(cloud_name)
        if previous and previous != supplier_name:
            logger.error(
                "Supplier names '%s' and '%s' resolve to the same pCloud folder",
                previous,
                supplier_name,
            )
            return 1
        folder_names[cloud_name] = supplier_name

    history = UploadHistory()
    pcloud = PCloudClient(
        api_host=PCLOUD_API_HOST,
        access_token=PCLOUD_ACCESS_TOKEN,
        root_folder_id=PCLOUD_ROOT_FOLDER_ID,
        timeout=PCLOUD_TIMEOUT_SECONDS,
    )
    imap = ImapClient(
        host=IMAP_HOST,
        port=IMAP_PORT,
        username=IMAP_USERNAME,
        password=IMAP_PASSWORD,
        oauth2_token=IMAP_OAUTH2_TOKEN,
        auth_method=IMAP_AUTH_METHOD,
        use_ssl=IMAP_USE_SSL,
        timeout=IMAP_TIMEOUT_SECONDS,
    )

    try:
        account = pcloud.test_connection()
        logger.info("pCloud connection validated for user ID %s", account.get("userid", "unknown"))
        imap.connect(IMAP_FOLDER)
        logger.info("IMAP connection validated; folder: %s", IMAP_FOLDER)
        stats = process_emails(
            imap,
            supplier_map,
            domain_map,
            pcloud,
            history,
            logger,
        )
        write_daily_path_log(EMAIL_AUTOMATION_PATH_LOG_FILE, stats["saved_paths"], logger)
        _log_summary(logger, stats, start_time)
        return 1 if stats["errors"] else 0
    finally:
        imap.close()
        pcloud.close()
        history.close()


def _log_summary(logger, stats: dict, start_time: datetime) -> None:
    logger.info(
        "Email automation summary: unseen=%d processed=%d uploaded=%d "
        "duplicates=%d no_supplier=%d no_pdf=%d errors=%d elapsed=%.1fs",
        stats["total_unread"],
        stats["processed"],
        stats["pdfs_uploaded"],
        stats["duplicates"],
        stats["skipped_no_supplier"],
        stats["skipped_no_pdf"],
        stats["errors"],
        (datetime.now() - start_time).total_seconds(),
    )
