"""IMAP email processing and supplier-to-pCloud routing."""

import logging
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from app.email_automation.attachment_handler import handle_pdf_attachment


def _received_datetime(message) -> datetime:
    try:
        value = parsedate_to_datetime(message.get("Date", ""))
        if value is None:
            raise ValueError
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc)


def _pdf_attachments(message):
    """Yield stable attachment key, filename, and decoded bytes."""
    index = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        content_type = part.get_content_type().lower()
        is_pdf = content_type == "application/pdf" or (
            filename and filename.lower().endswith(".pdf")
        )
        if not is_pdf:
            continue
        index += 1
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = b""
        yield str(index), filename or f"attachment_{index}.pdf", payload


def process_emails(
    imap_client,
    supplier_map: dict,
    domain_map: dict,
    pcloud_client,
    history,
    logger: logging.Logger,
) -> dict:
    """Process unseen IMAP messages and upload all matched PDFs safely."""
    stats = {
        "total_unread": 0,
        "processed": 0,
        "pdfs_uploaded": 0,
        "duplicates": 0,
        "skipped_no_supplier": 0,
        "skipped_no_pdf": 0,
        "errors": 0,
        "saved_paths": [],
    }
    uids = imap_client.unseen_uids()
    stats["total_unread"] = len(uids)
    logger.info("Found %d unseen email(s) to evaluate", len(uids))

    for uid in uids:
        try:
            imap_message = imap_client.fetch(uid)
            message_key = imap_message.key
            if history.is_message_processed(message_key):
                imap_client.mark_seen(uid)
                logger.info("UID %s was already processed; marked seen", uid)
                continue

            message = BytesParser(policy=policy.default).parsebytes(imap_message.raw_message)
            sender_email = parseaddr(str(message.get("From", "")))[1].strip().lower()
            subject = str(message.get("Subject", "(No Subject)"))
            logger.info("Processing UID %s: '%s' from %s", uid, subject, sender_email)

            supplier_name = supplier_map.get(sender_email)
            if not supplier_name:
                domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""
                supplier_name = domain_map.get(domain)
            if not supplier_name:
                logger.warning("  No supplier match for '%s'; leaving email unseen", sender_email)
                stats["skipped_no_supplier"] += 1
                continue

            attachments = list(_pdf_attachments(message))
            if not attachments:
                logger.info("  No PDF attachments; leaving email unseen")
                stats["skipped_no_pdf"] += 1
                continue

            received_at = _received_datetime(message)
            for attachment_key, filename, payload in attachments:
                if history.is_attachment_processed(message_key, attachment_key):
                    stats["duplicates"] += 1
                    continue
                result = handle_pdf_attachment(
                    payload=payload,
                    original_name=filename,
                    supplier_name=supplier_name,
                    received_at=received_at,
                    message_key=message_key,
                    attachment_key=attachment_key,
                    pcloud_client=pcloud_client,
                    history=history,
                    logger=logger,
                )
                if result.status == "uploaded":
                    stats["pdfs_uploaded"] += 1
                    stats["saved_paths"].append(result.cloud_path)
                else:
                    stats["duplicates"] += 1

            history.record_message(message_key)
            imap_client.mark_seen(uid)
            stats["processed"] += 1
            logger.info("  Done: all %d PDF(s) accounted for", len(attachments))
        except Exception as exc:
            stats["errors"] += 1
            logger.exception("Failed to process IMAP UID %s: %s", uid, exc)

    return stats
