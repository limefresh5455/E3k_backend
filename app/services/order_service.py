import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from app.db import get_conn
from app.services.erp_service import push_to_erp
from app.services.extraction_service import build_summary, extract_order_data, extract_text_from_bytes
from app.services.pcloud_service import pcloud_download_pdf, pcloud_get_folders, pcloud_get_view_url

semaphore = asyncio.Semaphore(2)
logger = logging.getLogger("order_service")


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


# ---------------------------------------------------------------------------
# Shared pipeline core
# ---------------------------------------------------------------------------

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
        pdf_text = extract_text_from_bytes(pdf_bytes)
        if not pdf_text.strip():
            raise ValueError("No text could be extracted (image-based PDF?)")
        logger.info("Text extracted for file_name=%s, chars=%d", file_name, len(pdf_text))

        # Step 2 - LLM extraction
        extracted = extract_order_data(pdf_text, pdf_bytes)
        order_number = str(extracted.get("OurOrderNumber", ""))
        supplier = extracted.get("Supplier", "Unknown")
        logger.info(
            "LLM extraction completed for file_name=%s, supplier=%s, order_number=%s, lines=%d",
            file_name,
            supplier,
            order_number,
            len(extracted.get("VoucherLines", [])),
        )
        # Step 3 - push to ERP
        erp_result = push_to_erp(extracted)
        logger.info(
            "ERP push succeeded for file_name=%s, supplier=%s, erp_record_id=%s, voucher_number=%s",
            file_name,
            supplier,
            erp_result.get("erp_record_id"),
            erp_result.get("voucher_number"),
        )

        updated_numbers = {
            str(n).strip().upper()
            for n in erp_result.get("payload_sent", {}).get("updated_pdf_numbers", [])
            if n
        }
        updated_line_totals = erp_result.get("payload_sent", {}).get("updated_line_totals", {}) or {}
        updated_line_quantities = erp_result.get("payload_sent", {}).get("updated_line_quantities", {}) or {}
        updated_line_erp_article_numbers = (
            erp_result.get("payload_sent", {}).get("updated_line_erp_article_numbers", {}) or {}
        )
        extracted_for_save = dict(extracted)
        if updated_numbers:
            filtered = []
            for ln in extracted.get("VoucherLines", []):
                num = str(ln.get("Number", "")).strip().upper()
                if num in updated_numbers:
                    ln_copy = dict(ln)
                    if num in updated_line_totals:
                        ln_copy["LineTotal"] = updated_line_totals[num]
                    if num in updated_line_quantities:
                        ln_copy["Quantity"] = updated_line_quantities[num]
                    if num in updated_line_erp_article_numbers:
                        ln_copy["ErpArticleNumber"] = updated_line_erp_article_numbers[num]
                    filtered.append(ln_copy)
            extracted_for_save["VoucherLines"] = filtered
        else:
            extracted_for_save["VoucherLines"] = extracted.get("VoucherLines", [])

        summary = build_summary(extracted_for_save, file_name, folder_name)
        erp_alerts = erp_result.get("payload_sent", {}).get("alerts", []) or []
        summary["alerts"] = erp_alerts
        summary["requires_double_check"] = bool(erp_result.get("payload_sent", {}).get("requires_double_check"))

        # attention is intentionally narrower than requires_double_check / alerts above.
        # Those keep working exactly as before (unit-factor, quantity, surcharge,
        # delivery-date-over-a-week all still show up as warnings on the order).
        # attention is ONLY set for the two things that should actually pull the client's
        # eye to the dashboard:
        #   1. format/pricing mismatch  -> PDF total doesn't match the calculated total
        #   2. fewer lines than expected -> some PDF/PO lines are missing or were only
        #      recovered via the fallback text-table parser
        _ATTENTION_ALERT_TYPES = {
            "pdf_total_mismatch",
            "fewer_lines_than_expected",
            "lines_recovered_via_fallback",
        }
        attention_alerts = [a for a in erp_alerts if a.get("type") in _ATTENTION_ALERT_TYPES]
        attention = bool(attention_alerts)
        attention_reasons = [a.get("message", a.get("type", "")) for a in attention_alerts]
        order_status = "attention" if attention else "success"

        # Step 4 - save success
        _save_success(
            file_id=file_id,
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
        mark_as_processed(file_id, file_name)

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
        raise ValueError(f"Order id {order_id} not found.")

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
            f"No line with erp_article_no='{erp_article_no}' found on order id {order_id}. "
            "The ERP was still updated; only the dashboard record could not be synced."
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