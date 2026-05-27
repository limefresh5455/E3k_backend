import asyncio
import hashlib
import json
import logging
import time

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
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders
            (file_id, file_name, folder_name, pdf_url, order_number,
             supplier, status, extracted_json, summary,
             erp_record_id, erp_voucher_number, erp_supplier_number)
        VALUES (%s, %s, %s, %s, %s, %s, 'success', %s, %s, %s, %s, %s)
        ON CONFLICT (file_id) DO UPDATE SET
            status='success',
            extracted_json=EXCLUDED.extracted_json,
            summary=EXCLUDED.summary,
            erp_record_id=EXCLUDED.erp_record_id,
            erp_voucher_number=EXCLUDED.erp_voucher_number,
            erp_supplier_number=EXCLUDED.erp_supplier_number,
            processed_at=NOW()
        """,
        (
            file_id,
            file_name,
            folder_name,
            pdf_url,
            order_number,
            supplier,
            json.dumps(extracted),
            json.dumps(summary),
            erp_record_id,
            erp_voucher_number,
            erp_supplier_number,
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
        extracted_for_save = dict(extracted)
        if updated_numbers:
            extracted_for_save["VoucherLines"] = [
                ln for ln in extracted.get("VoucherLines", [])
                if str(ln.get("Number", "")).strip().upper() in updated_numbers
            ]
        else:
            extracted_for_save["VoucherLines"] = extracted.get("VoucherLines", [])

        summary = build_summary(extracted_for_save, file_name, folder_name)
        erp_alerts = erp_result.get("payload_sent", {}).get("alerts", []) or []
        summary["alerts"] = erp_alerts
        summary["requires_double_check"] = bool(erp_result.get("payload_sent", {}).get("requires_double_check"))

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
        )
        mark_as_processed(file_id, file_name)

        return {
            "status": "success",
            "order_number": order_number,
            "supplier": supplier,
            "erp_record_id": erp_result.get("erp_record_id"),
            "erp_voucher_number": erp_result.get("voucher_number"),
            "erp_supplier_number": erp_result.get("supplier_number"),
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
        return {"status": "failure", "error": str(error)}


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
               summary, erp_record_id, erp_voucher_number, erp_supplier_number,
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


def get_stats():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
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
