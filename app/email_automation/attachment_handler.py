"""Checksum, deduplicate, and upload IMAP PDF attachments."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from app.email_automation.pcloud_upload_client import safe_cloud_name


@dataclass(frozen=True)
class AttachmentResult:
    status: str
    cloud_path: str
    sha256: str


def handle_pdf_attachment(
    payload: bytes,
    original_name: str,
    supplier_name: str,
    received_at: datetime,
    message_key: str,
    attachment_key: str,
    pcloud_client,
    history,
    logger: logging.Logger,
) -> AttachmentResult:
    """Upload one PDF idempotently and return its cloud status."""
    if b"%PDF-" not in payload[:1024]:
        raise ValueError(f"Attachment '{original_name}' is not a valid PDF payload")
    sha256 = hashlib.sha256(payload).hexdigest()
    sha1 = hashlib.sha1(payload).hexdigest()
    supplier_key = safe_cloud_name(supplier_name, "Unknown Supplier")

    previous = history.find_upload(supplier_key, sha256, sha1)
    if previous is not None:
        cloud_path = str(previous["cloud_path"])
        history.record_attachment(message_key, attachment_key, sha256, cloud_path)
        logger.info("  Duplicate PDF skipped: %s", original_name)
        return AttachmentResult("duplicate", cloud_path, sha256)

    safe_original = safe_cloud_name(Path(original_name).name, "attachment.pdf")
    date_prefix = received_at.strftime("%Y-%m-%d")
    cloud_filename = f"{date_prefix}_{sha256[:12]}_{safe_original}"

    with TemporaryDirectory(prefix="email_automation_") as temporary_directory:
        temporary_path = Path(temporary_directory) / safe_original
        temporary_path.write_bytes(payload)
        if temporary_path.stat().st_size == 0:
            raise ValueError(f"Attachment '{original_name}' is empty")

        upload = pcloud_client.upload_pdf(
            temporary_path,
            supplier_name,
            cloud_filename,
            sha1,
            sha256,
        )

    history.record_upload(
        sha256=sha256,
        sha1=sha1,
        original_filename=original_name,
        supplier=supplier_key,
        cloud_path=upload.cloud_path,
        pcloud_file_id=upload.file_id,
        size=len(payload),
    )
    history.record_attachment(message_key, attachment_key, sha256, upload.cloud_path)
    logger.info("  %s: %s", upload.status.capitalize(), upload.cloud_path)
    return AttachmentResult(upload.status, upload.cloud_path, sha256)
