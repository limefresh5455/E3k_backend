import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.db import get_conn
from app.services.erp_service import push_to_erp
from app.services.extraction_service import (
    build_summary,
    extract_order_data,
    extract_text_from_bytes,
    split_mcc_combined_order_data,
)
from app.services.pcloud_service import (
    pcloud_download_pdf,
    pcloud_get_file_preview_url,
    pcloud_get_folders,
    pcloud_get_view_url,
)

semaphore = asyncio.Semaphore(2)
logger = logging.getLogger("order_service")


class OrderPdfNotAvailableError(RuntimeError):
    """Raised when an order does not point to a PDF stored in pCloud."""


def is_already_processed(file_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    # A file is considered already processed only if it exists in BOTH:
    # 1) processed_files guard table, and
    # 2) orders dashboard table.
    # This allows re-processing when orders were manually deleted.
    cur.execute(
        """
        SELECT 1
        FROM processed_files pf
        WHERE pf.file_id = %s
          AND EXISTS (
              SELECT 1 FROM orders o WHERE o.file_id = pf.file_id
          )
        """,
        (file_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row is not None


def mark_as_processed(file_id: str, file_name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO processed_files (file_id, file_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (file_id, file_name),
    )
    conn.commit()
    cur.close()
    conn.close()


def _save_success(
    *,
    file_id: str,
    file_name: str,
    folder_name: str,
    pdf_url: str,
    order_number: str,
    supplier: str,
    extracted: dict,
    summary: dict,
    # ERP fields — optional so existing callers don't break
    erp_record_id: str = "",
    erp_voucher_number: str = "",
    erp_supplier_number: str = "",
    erp_article_no: str = "",
    # New: real numeric-mismatch review flag — optional so existing callers don't break
    attention: bool = False,
    attention_reasons: list = None,
    # status is 'success' normally, or 'attention' when attention=True — needs manual review
    status: str = "success",
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders
            (file_id, file_name, folder_name, pdf_url, order_number,
             supplier, status, extracted_json, summary,
             erp_record_id, erp_voucher_number, erp_supplier_number, erp_article_no,
             attention, attention_reasons)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (file_id) DO UPDATE SET
            status=EXCLUDED.status,
            extracted_json=EXCLUDED.extracted_json,
            summary=EXCLUDED.summary,
            erp_record_id=EXCLUDED.erp_record_id,
            erp_voucher_number=EXCLUDED.erp_voucher_number,
            erp_supplier_number=EXCLUDED.erp_supplier_number,
            erp_article_no=EXCLUDED.erp_article_no,
            attention=EXCLUDED.attention,
            attention_reasons=EXCLUDED.attention_reasons,
            processed_at=NOW()
        """,
        (
            file_id,
            file_name,
            folder_name,
            pdf_url,
            order_number,
            supplier,
            status,
            json.dumps(extracted),
            json.dumps(summary),
            erp_record_id,
            erp_voucher_number,
            erp_supplier_number,
            erp_article_no,
            attention,
            json.dumps(attention_reasons or []),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()


def _save_failure(
    file_id: str,
    file_name: str,
    folder_name: str,
    pdf_url: str,
    error_message: str,
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders
            (file_id, file_name, folder_name, pdf_url, status, error_message)
        VALUES (%s, %s, %s, %s, 'failure', %s)
        ON CONFLICT (file_id) DO UPDATE SET
            status='failure',
            error_message=EXCLUDED.error_message,
            processed_at=NOW()
        """,
        (file_id, file_name, folder_name, pdf_url, error_message),
    )
    conn.commit()
    cur.close()
    conn.close()


def _save_skipped(
    file_id: str,
    file_name: str,
    folder_name: str,
    pdf_url: str,
    reason: str,
):
    summary = {
        "file_name": file_name,
        "folder": folder_name,
        "supplier": "SCHLAUCHSERVICE",
        "line_count": 0,
        "total_net": 0,
        "lines": [],
        "skip_reason": reason,
    }
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders
            (file_id, file_name, folder_name, pdf_url, supplier, status,
             error_message, summary, erp_article_no, attention, attention_reasons)
        VALUES (%s, %s, %s, %s, 'SCHLAUCHSERVICE', 'skipped', %s, %s, '', FALSE, '[]'::jsonb)
        ON CONFLICT (file_id) DO UPDATE SET
            status='skipped',
            file_name=EXCLUDED.file_name,
            folder_name=EXCLUDED.folder_name,
            pdf_url=EXCLUDED.pdf_url,
            order_number=NULL,
            supplier='SCHLAUCHSERVICE',
            error_message=EXCLUDED.error_message,
            extracted_json=NULL,
            summary=EXCLUDED.summary,
            erp_record_id=NULL,
            erp_voucher_number=NULL,
            erp_supplier_number=NULL,
            erp_article_no='',
            attention=FALSE,
            attention_reasons='[]'::jsonb,
            processed_at=NOW()
        """,
        (file_id, file_name, folder_name, pdf_url, reason, json.dumps(summary)),
    )
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Shared pipeline core
# ---------------------------------------------------------------------------


def _process_extracted_order(
    *,
    extracted: dict,
    persistence_file_id: str,
    file_name: str,
    folder_name: str,
    pdf_url: str,
) -> dict:
    """Run the unchanged ERP/save flow for one logical extracted order."""
    order_number = str(extracted.get("OurOrderNumber", ""))
    supplier = extracted.get("Supplier", "Unknown")
    logger.info(
        "Processing extracted order: file_name=%s, supplier=%s, order_number=%s, lines=%d",
        file_name,
        supplier,
        order_number,
        len(extracted.get("VoucherLines", [])),
    )

    erp_result = push_to_erp(extracted)
    logger.info(
        "ERP push succeeded for file_name=%s, supplier=%s, erp_record_id=%s, voucher_number=%s",
        file_name,
        supplier,
        erp_result.get("erp_record_id"),
        erp_result.get("voucher_number"),
    )

    updated_numbers = {
        str(number).strip().upper()
        for number in erp_result.get("payload_sent", {}).get("updated_pdf_numbers", [])
        if number
    }
    updated_line_totals = (
        erp_result.get("payload_sent", {}).get("updated_line_totals", {}) or {}
    )
    updated_line_quantities = (
        erp_result.get("payload_sent", {}).get("updated_line_quantities", {}) or {}
    )
    updated_line_erp_article_numbers = (
        erp_result.get("payload_sent", {}).get("updated_line_erp_article_numbers", {}) or {}
    )
    extracted_for_save = dict(extracted)
    if updated_numbers:
        filtered = []
        for line in extracted.get("VoucherLines", []):
            number = str(line.get("Number", "")).strip().upper()
            if number not in updated_numbers:
                continue
            line_copy = dict(line)
            if number in updated_line_totals:
                line_copy["LineTotal"] = updated_line_totals[number]
            if number in updated_line_quantities:
                line_copy["Quantity"] = updated_line_quantities[number]
            if number in updated_line_erp_article_numbers:
                line_copy["ErpArticleNumber"] = updated_line_erp_article_numbers[number]
            filtered.append(line_copy)
        extracted_for_save["VoucherLines"] = filtered
    else:
        extracted_for_save["VoucherLines"] = extracted.get("VoucherLines", [])

    summary = build_summary(extracted_for_save, file_name, folder_name)
    erp_alerts = erp_result.get("payload_sent", {}).get("alerts", []) or []
    summary["alerts"] = erp_alerts
    summary["requires_double_check"] = bool(
        erp_result.get("payload_sent", {}).get("requires_double_check")
    )

    attention_alert_types = {
        "pdf_total_mismatch",
        "fewer_lines_than_expected",
        "mcc_discount_differs_from_default",
        "mcc_sales_price_update_failed",
        "mcc_sales_price_validation_failed",
    }
    attention_alerts = [
        alert for alert in erp_alerts if alert.get("type") in attention_alert_types
    ]
    attention = bool(attention_alerts)
    attention_reasons = [
        alert.get("message", alert.get("type", "")) for alert in attention_alerts
    ]
    order_status = "attention" if attention else "success"

    _save_success(
        file_id=persistence_file_id,
        file_name=file_name,
        folder_name=folder_name,
        pdf_url=pdf_url,
        order_number=order_number,
        supplier=supplier,
        extracted=extracted_for_save,
        summary=summary,
        erp_record_id=erp_result.get("erp_record_id", ""),
        erp_voucher_number=erp_result.get("voucher_number", ""),
        erp_supplier_number=erp_result.get("supplier_number", ""),
        erp_article_no=erp_result.get("erp_article_numbers", ""),
        attention=attention,
        attention_reasons=attention_reasons,
        status=order_status,
    )
    retry_mcc_sales_prices = any(
        alert.get("type") == "mcc_sales_price_update_failed"
        for alert in erp_alerts
    )
    return {
        "status": order_status,
        "attention": attention,
        "attention_reasons": attention_reasons,
        "order_number": order_number,
        "supplier": supplier,
        "erp_record_id": erp_result.get("erp_record_id"),
        "erp_voucher_number": erp_result.get("voucher_number"),
        "erp_supplier_number": erp_result.get("supplier_number"),
        "erp_article_no": erp_result.get("erp_article_numbers"),
        "retry_required": retry_mcc_sales_prices,
    }


def _run_pipeline(
    pdf_bytes: bytes,
    file_id: str,
    file_name: str,
    folder_name: str,
    pdf_url: str,
) -> dict:
    """
    Full pipeline:
      1. Extract text from PDF bytes
      2. LLM extraction
      3. Push to ERP (resolve supplier -> create PurchaseOrder)
      4. Save result to DB
      5. Return result dict
    """
    try:
        logger.info(
            "Starting PDF pipeline: file_id=%s, file_name=%s, folder=%s, pdf_url=%s",
            file_id,
            file_name,
            folder_name,
            pdf_url,
        )
        # Step 1 - extract text
        pdf_text, blocked_logo_found = extract_text_from_bytes(
            pdf_bytes,
            include_logo_detection=True,
        )
        if blocked_logo_found:
            reason = "SCHLAUCHSERVICE logo detected in embedded-image OCR"
            logger.warning(
                "Skipping PDF before LLM/ERP processing: file_id=%s, file_name=%s, reason=%s",
                file_id,
                file_name,
                reason,
            )
            _save_skipped(file_id, file_name, folder_name, pdf_url, reason)
            mark_as_processed(file_id, file_name)
            return {
                "status": "skipped",
                "attention": False,
                "attention_reasons": [],
                "reason": reason,
            }
        if not pdf_text.strip():
            raise ValueError("Es konnte kein Text extrahiert werden (bildbasiertes PDF?)")
        logger.info("Text extracted for file_name=%s, chars=%d", file_name, len(pdf_text))

        # Step 2 - run the existing extraction once, then split only a strictly
        # recognized combined MCC confirmation into logical purchase orders.
        extracted = extract_order_data(pdf_text, pdf_bytes)
        extracted_orders = split_mcc_combined_order_data(extracted)
        logger.info(
            "Extraction completed for file_name=%s, supplier=%s, logical_orders=%s, lines=%d",
            file_name,
            extracted.get("Supplier", "Unknown"),
            [order.get("OurOrderNumber") for order in extracted_orders],
            len(extracted.get("VoucherLines", [])),
        )

        if len(extracted_orders) == 1:
            result = _process_extracted_order(
                extracted=extracted_orders[0],
                persistence_file_id=file_id,
                file_name=file_name,
                folder_name=folder_name,
                pdf_url=pdf_url,
            )
            if not result["retry_required"]:
                mark_as_processed(file_id, file_name)
            return result

        # One physical MCC PDF can represent multiple ERP purchase orders. The
        # first row keeps the source file ID so existing processed-file checks
        # remain unchanged; additional dashboard rows use deterministic child
        # IDs and still point to the same source PDF.
        child_results: list[dict] = []
        for index, child in enumerate(extracted_orders):
            child_order_number = str(child.get("OurOrderNumber") or "").strip()
            child_file_id = (
                file_id
                if index == 0
                else f"{file_id}:mcc:{child_order_number}"
            )
            try:
                child_result = _process_extracted_order(
                    extracted=child,
                    persistence_file_id=child_file_id,
                    file_name=file_name,
                    folder_name=folder_name,
                    pdf_url=pdf_url,
                )
            except Exception as child_error:
                logger.exception(
                    "Combined MCC child failed: file_name=%s, order_number=%s",
                    file_name,
                    child_order_number,
                )
                _save_failure(
                    child_file_id,
                    file_name,
                    folder_name,
                    pdf_url,
                    str(child_error),
                )
                child_result = {
                    "status": "failure",
                    "attention": False,
                    "attention_reasons": [],
                    "order_number": child_order_number,
                    "supplier": child.get("Supplier", "Unknown"),
                    "retry_required": False,
                    "error": str(child_error),
                }
            child_results.append(child_result)

        retry_required = any(result.get("retry_required") for result in child_results)
        if not retry_required:
            mark_as_processed(file_id, file_name)

        statuses = {result["status"] for result in child_results}
        overall_status = (
            "failure"
            if "failure" in statuses
            else "attention"
            if "attention" in statuses
            else "success"
        )
        attention_reasons = [
            reason
            for result in child_results
            for reason in result.get("attention_reasons", [])
        ]
        return {
            "status": overall_status,
            "attention": overall_status == "attention",
            "attention_reasons": attention_reasons,
            "supplier": extracted.get("Supplier", "Unknown"),
            "order_count": len(child_results),
            "orders": child_results,
            "retry_required": retry_required,
        }

    except Exception as error:
        logger.exception(
            "PDF pipeline failed: file_id=%s, file_name=%s, folder=%s, error=%s",
            file_id,
            file_name,
            folder_name,
            str(error),
        )
        try:
            _save_failure(file_id, file_name, folder_name, pdf_url, str(error))
            mark_as_processed(file_id, file_name)
        except Exception:
            logger.exception(
                "Failed to persist failure state: file_id=%s, file_name=%s",
                file_id,
                file_name,
            )
            pass
        return {"status": "failure", "attention": False, "attention_reasons": [], "error": str(error)}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def process_local_file(file_path: str, file_id: str, file_name: str, folder_name: str) -> dict:
    pdf_url = f"local://{file_name}"
    with open(file_path, "rb") as f:
        pdf_bytes = f.read()
    return _run_pipeline(pdf_bytes, file_id, file_name, folder_name, pdf_url)


def process_file(file_id: str, file_name: str, folder_name: str) -> dict:
    pdf_url = pcloud_get_view_url(file_id)
    pdf_bytes = pcloud_download_pdf(file_id)
    return _run_pipeline(pdf_bytes, file_id, file_name, folder_name, pdf_url)


def process_pdf_bytes(pdf_bytes: bytes, file_name: str) -> dict:
    """
    Used by the manual upload endpoint in sync.py.
    Generates a unique file_id so it never collides with pCloud entries.
    No duplicate-check applied — user explicitly uploaded it.
    """
    file_id = hashlib.md5(f"{file_name}{time.time()}".encode()).hexdigest()
    pdf_url = f"upload://{file_name}"
    return _run_pipeline(pdf_bytes, file_id, file_name, folder_name="manual_upload", pdf_url=pdf_url)


async def process_wrapper(file_id: str, file_name: str, folder_name: str):
    async with semaphore:
        try:
            result = await asyncio.to_thread(process_file, file_id, file_name, folder_name)
            return {"file": file_name, "folder": folder_name, **result}
        except Exception as error:
            return {"file": file_name, "folder": folder_name, "status": "failure", "error": str(error)}


# ---------------------------------------------------------------------------
# DB read helpers (unchanged - used by orders.py)
# ---------------------------------------------------------------------------

def list_orders():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, file_id, file_name, folder_name, pdf_url,
               order_number, supplier, status, error_message,
               summary, erp_record_id, erp_voucher_number, erp_supplier_number, erp_article_no,
               attention, attention_reasons,
               processed_at
        FROM orders
        ORDER BY processed_at DESC
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(row) for row in rows]


def get_order(order_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def get_order_by_number(order_number: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE order_number = %s", (order_number,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def get_order_pdf_view(order_id: int):
    """Return safe pCloud viewer metadata for an order, if it has a pCloud PDF."""
    order = get_order(order_id)
    if not order:
        return None

    stored_url = str(order.get("pdf_url") or "").strip()
    parsed_url = urlsplit(stored_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "e.pcloud.com":
        raise OrderPdfNotAvailableError(
            "This order's PDF is not stored in pCloud."
        )

    file_id = str(order.get("file_id") or "").strip()
    # Additional logical orders from one combined MCC PDF use deterministic
    # child IDs in the dashboard, but all of them open the same source PDF.
    if ":mcc:" in file_id:
        file_id = file_id.split(":mcc:", 1)[0]
    if not file_id.isdecimal():
        raise OrderPdfNotAvailableError(
            "This order does not have a valid pCloud file ID."
        )

    return {
        "order_id": int(order["id"]),
        "file_name": str(order.get("file_name") or ""),
        # Open exactly this PDF in pCloud's standalone document viewer.
        "view_url": pcloud_get_file_preview_url(file_id),
    }


def update_order_line_after_manual_correction(
    order_id: int,
    erp_article_no: str,
    *,
    unit_price: float,
    line_total: float,
    discount_percent: float = None,
    delivery_date: str = None,
) -> dict:
    """
    After a manual /api/update-order-line push to the ERP succeeds, reflect the
    corrected values back into the stored order row - both extracted_json.VoucherLines
    and summary.lines - so the dashboard shows the fix without needing a re-sync.

    Matched by ErpArticleNumber (the ERP article number, F003), NOT by the PDF's
    own line "Number" field, since erp_article_no is the value the user actually
    provided and is guaranteed present on every line the pipeline has pushed to ERP.
    order_id is used to locate the row since it's unique.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = %s FOR UPDATE", (order_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise ValueError(f"Auftrag mit der ID {order_id} wurde nicht gefunden.")

    order = dict(row)
    extracted = order.get("extracted_json") or {}
    summary = order.get("summary") or {}
    target = str(erp_article_no or "").strip().upper()

    matched_line = None
    for ln in extracted.get("VoucherLines", []):
        if str(ln.get("ErpArticleNumber", "")).strip().upper() == target:
            matched_line = ln
            break

    if matched_line is None:
        cur.close()
        conn.close()
        raise ValueError(
            f"Im Auftrag mit der ID {order_id} wurde keine Zeile mit erp_article_no='{erp_article_no}' gefunden. "
            "Das ERP wurde trotzdem aktualisiert; nur der Dashboard-Datensatz konnte nicht synchronisiert werden."
        )

    matched_line["GrossPrice"] = unit_price
    matched_line["LineTotal"] = line_total
    if discount_percent is not None:
        matched_line["DiscountPercent"] = discount_percent
    if delivery_date:
        matched_line["DeliveryDate"] = delivery_date
    matched_line["ManuallyCorrectedAt"] = datetime.now(timezone.utc).isoformat()

    # Keep summary.lines in sync too (matched via the PDF number on the line we just found).
    pdf_number = str(matched_line.get("Number", "")).strip()
    for summary_line in summary.get("lines", []):
        if str(summary_line.get("number", "")).strip() == pdf_number:
            summary_line["unit_price"] = round(float(unit_price), 2)
            summary_line["line_total"] = round(float(line_total), 2)
            if discount_percent is not None:
                summary_line["discount_percent"] = discount_percent
            if delivery_date:
                summary_line["delivery_date"] = delivery_date
            break

    summary["total_net"] = round(
        sum(float(sl.get("line_total") or 0) for sl in summary.get("lines", [])), 2
    )

    cur.execute(
        "UPDATE orders SET extracted_json = %s, summary = %s, processed_at = NOW() WHERE id = %s",
        (json.dumps(extracted), json.dumps(summary), order_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return order


def get_stats():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
            SUM(CASE WHEN status='attention' THEN 1 ELSE 0 END) AS attention,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
            SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure,
            COUNT(DISTINCT supplier) AS suppliers
        FROM orders
        """
    )
    row = dict(cur.fetchone())
    cur.close()
    conn.close()
    return row


async def get_pcloud_folders():
    return await asyncio.to_thread(pcloud_get_folders)
