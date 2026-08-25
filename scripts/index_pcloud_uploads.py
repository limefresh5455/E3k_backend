"""One-time index of existing pCloud PDFs for duplicate prevention."""

import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (
    PCLOUD_ACCESS_TOKEN,
    PCLOUD_API_HOST,
    PCLOUD_ROOT_FOLDER_ID,
    PCLOUD_TIMEOUT_SECONDS,
)
from app.email_automation.config import validate_pcloud_configuration
from app.email_automation.database import (
    EmailAutomationLock,
    JobAlreadyRunning,
    init_email_automation_db,
)
from app.email_automation.logger import setup_logging
from app.email_automation.pcloud_upload_client import PCloudClient
from app.email_automation.repository import UploadHistory


def _files(metadata, supplier=""):
    for item in metadata.get("contents", []):
        if item.get("isfolder"):
            yield from _files(item, supplier or str(item.get("name", "")))
        elif str(item.get("name", "")).lower().endswith(".pdf"):
            yield supplier or "Existing pCloud folder", item


def main() -> int:
    logger = setup_logging()
    errors = validate_pcloud_configuration()
    if errors:
        for error in errors:
            logger.error("Configuration: %s", error)
        return 1

    try:
        init_email_automation_db()
        with EmailAutomationLock():
            return _index(logger)
    except JobAlreadyRunning as exc:
        logger.warning("%s", exc)
        return 2
    except Exception as exc:
        logger.exception("pCloud indexing failed: %s", exc)
        return 1


def _index(logger: logging.Logger) -> int:
    history = UploadHistory()
    client = PCloudClient(
        PCLOUD_API_HOST,
        PCLOUD_ACCESS_TOKEN,
        PCLOUD_ROOT_FOLDER_ID,
        PCLOUD_TIMEOUT_SECONDS,
    )
    indexed = 0
    try:
        client.test_connection()
        for supplier, metadata in _files(client.list_tree()):
            checksums = client.checksums(int(metadata["fileid"]))
            sha1 = checksums.get("sha1", "")
            if not sha1:
                logger.warning("No SHA-1 returned for %s", metadata.get("path", metadata["name"]))
                continue
            sha256 = checksums.get("sha256") or f"sha1:{sha1}"
            history.record_upload(
                sha256=sha256,
                sha1=sha1,
                original_filename=str(metadata["name"]),
                supplier=supplier,
                cloud_path=str(metadata.get("path", metadata["name"])),
                pcloud_file_id=int(metadata["fileid"]),
                size=int(metadata.get("size", 0)),
            )
            indexed += 1
        logger.info("Indexed %d existing pCloud PDF(s)", indexed)
        return 0
    finally:
        client.close()
        history.close()


if __name__ == "__main__":
    sys.exit(main())
