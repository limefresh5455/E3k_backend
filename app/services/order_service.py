import asyncio
import json

from app.db import get_conn
from app.services.extraction_service import build_summary, extract_text_from_bytes, llm_extract
from app.services.pcloud_service import pcloud_download_pdf, pcloud_get_folders, pcloud_get_view_url

semaphore = asyncio.Semaphore(2)


def is_already_processed(file_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM processed_files WHERE file_id = %s", (file_id,))
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
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO orders
            (file_id, file_name, folder_name, pdf_url, order_number,
             supplier, status, extracted_json, summary)
        VALUES (%s, %s, %s, %s, %s, %s, 'success', %s, %s)
        ON CONFLICT (file_id) DO UPDATE SET
            status='success',
            extracted_json=EXCLUDED.extracted_json,
            summary=EXCLUDED.summary,
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
        ),
    )
    conn.commit()
    cur.close()
    conn.close()


def _save_failure(file_id: str, file_name: str, folder_name: str, pdf_url: str, error_message: str):
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


def process_local_file(file_path: str, file_id: str, file_name: str, folder_name: str):
    pdf_url = f"local://{file_name}"
    try:
        with open(file_path, "rb") as file:
            pdf_bytes = file.read()

        pdf_text = extract_text_from_bytes(pdf_bytes)
        if not pdf_text.strip():
            raise ValueError("No text extracted")

        extracted = llm_extract(pdf_text)
        order_number = str(extracted.get("OurOrderNumber", ""))
        supplier = extracted.get("Supplier", "Unknown")
        summary = build_summary(extracted, file_name, folder_name)

        _save_success(
            file_id=file_id,
            file_name=file_name,
            folder_name=folder_name,
            pdf_url=pdf_url,
            order_number=order_number,
            supplier=supplier,
            extracted=extracted,
            summary=summary,
        )
        mark_as_processed(file_id, file_name)
        return {"status": "success", "order_number": order_number, "supplier": supplier}
    except Exception as error:
        try:
            _save_failure(file_id, file_name, folder_name, pdf_url, str(error))
            mark_as_processed(file_id, file_name)
        except Exception:
            pass
        return {"status": "failure", "error": str(error)}


def process_file(file_id: str, file_name: str, folder_name: str) -> dict:
    pdf_url = pcloud_get_view_url(file_id)
    try:
        pdf_bytes = pcloud_download_pdf(file_id)
        pdf_text = extract_text_from_bytes(pdf_bytes)
        if not pdf_text.strip():
            raise ValueError("No text could be extracted (image-based PDF?)")

        extracted = llm_extract(pdf_text)
        order_number = str(extracted.get("OurOrderNumber", ""))
        supplier = extracted.get("Supplier", "Unknown")
        summary = build_summary(extracted, file_name, folder_name)

        _save_success(
            file_id=file_id,
            file_name=file_name,
            folder_name=folder_name,
            pdf_url=pdf_url,
            order_number=order_number,
            supplier=supplier,
            extracted=extracted,
            summary=summary,
        )
        mark_as_processed(file_id, file_name)
        return {"status": "success", "order_number": order_number, "supplier": supplier}
    except Exception as error:
        try:
            _save_failure(file_id, file_name, folder_name, pdf_url, str(error))
            mark_as_processed(file_id, file_name)
        except Exception:
            pass
        return {"status": "failure", "error": str(error)}


async def process_wrapper(file_id: str, file_name: str, folder_name: str):
    async with semaphore:
        try:
            result = await asyncio.to_thread(process_file, file_id, file_name, folder_name)
            return {"file": file_name, "folder": folder_name, **result}
        except Exception as error:
            return {"file": file_name, "folder": folder_name, "status": "failure", "error": str(error)}


def list_orders():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, file_id, file_name, folder_name, pdf_url,
               order_number, supplier, status, error_message,
               summary, processed_at
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

