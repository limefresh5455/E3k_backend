import asyncio
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import LOCAL_PDF_MODE, OPENAI_API_KEY
from app.services.erp_service import push_manual_line_update
from app.services.order_service import (
    get_order,
    get_order_by_number,
    get_pcloud_folders,
    is_already_processed,
    process_file,
    process_local_file,
    process_pdf_bytes,
    update_order_line_after_manual_correction,
)
from app.services.pcloud_service import get_local_pdfs

router = APIRouter()


@router.post("/api/sync")
async def sync_pcloud():
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    results = {
        "total_found": 0,
        "skipped": 0,
        "processed": 0,
        "success": 0,
        "attention": 0,
        "failure": 0,
        "details": [],
    }
    tasks = []

    if LOCAL_PDF_MODE:
        try:
            files = get_local_pdfs()
        except Exception:
            files = []

        for file_data in files:
            file_id = file_data["file_id"]
            file_name = file_data["file_name"]
            folder_name = file_data["folder_name"]
            file_path = file_data["file_path"]

            results["total_found"] += 1
            already = await asyncio.to_thread(is_already_processed, file_id)
            if already:
                results["skipped"] += 1
                continue

            tasks.append(
                asyncio.to_thread(
                    process_local_file,
                    file_path,
                    file_id,
                    file_name,
                    folder_name,
                )
            )

    # Default and fallback path: process directly from pCloud bytes
    if (not LOCAL_PDF_MODE) or (LOCAL_PDF_MODE and not tasks):
        folders = await get_pcloud_folders()
        for folder in folders:
            if not folder.get("isfolder"):
                continue

            folder_name = folder["name"]
            for item in folder.get("contents", []):
                if item.get("isfolder") or not item["name"].lower().endswith(".pdf"):
                    continue

                results["total_found"] += 1
                file_id = str(item["fileid"])
                file_name = item["name"]

                already = await asyncio.to_thread(is_already_processed, file_id)
                if already:
                    results["skipped"] += 1
                    continue

                tasks.append(asyncio.to_thread(process_file, file_id, file_name, folder_name))

    responses = await asyncio.gather(*tasks, return_exceptions=True)
    for response in responses:
        results["processed"] += 1

        if isinstance(response, Exception):
            results["failure"] += 1
            continue

        if response["status"] == "success":
            results["success"] += 1
        elif response["status"] == "attention":
            results["attention"] += 1
        else:
            results["failure"] += 1

        results["details"].append(response)

    return results


@router.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    """
    Upload a single supplier PDF manually.

    Pipeline:
      1. Extract text from PDF
      2. LLM extracts structured order data
      3. Supplier is resolved in the ERP address master
      4. PurchaseOrder is created in europa3000
      5. Result is saved to the orders dashboard

    Returns:
      {
        "status": "success",
        "order_number": "2600364",
        "supplier": "TRELLEBORG CLERMONT-FERRAND SAS",
        "erp_record_id": "8661",
        "erp_voucher_number": "2600364",
        "erp_supplier_number": "001977"
      }
    """
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not set")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    result = await asyncio.to_thread(process_pdf_bytes, pdf_bytes, file.filename)

    if result.get("status") == "failure":
        raise HTTPException(status_code=422, detail=result)

    return result


class UpdateOrderLineRequest(BaseModel):
    order_id: Optional[int] = Field(
        None, description="Unique dashboard order id (preferred way to locate the order). "
        "Provide this or voucher_number."
    )
    voucher_number: Optional[str] = Field(
        None, description="Order/voucher number as shown on the PDF, e.g. '2601018'. "
        "Used to locate the order if order_id isn't given. "
        "The 'B' prefix required by the ERP (e.g. 'B2601018') is added automatically — do not include it."
    )
    erp_article_no: str = Field(
        ..., description="ERP article number for the line being corrected. Sent to the ERP as F003, "
        "and also used to find/update the matching line already stored on the order."
    )
    unit_price: float = Field(..., description="Corrected unit price.")
    total: float = Field(..., description="Corrected line total.")
    discount_percent: Optional[float] = Field(None, description="Corrected discount percent, if any.")
    delivery_date: Optional[str] = Field(
        None, description="Corrected delivery date, 'DD.MM.YYYY' or 'YYYY-MM-DD'. Omit to leave unchanged."
    )


@router.post("/api/update-order-line")
async def update_order_line(payload: UpdateOrderLineRequest):
    """
    Manual dashboard correction: push user-entered values for a single ERP
    purchase-order line directly to europa3000, then sync the same values
    back into the stored order row so the dashboard reflects the fix.

    The order is located by order_id (preferred, unique) or voucher_number.
    The specific line within that order is located by erp_article_no —
    NOT by the PDF's own line number — since that's the value guaranteed
    to already be stored on every line the pipeline has pushed to the ERP.
    """
    if not payload.order_id and not payload.voucher_number:
        raise HTTPException(status_code=400, detail="Provide either order_id or voucher_number.")

    if payload.order_id:
        order = await asyncio.to_thread(get_order, payload.order_id)
        if not order:
            raise HTTPException(status_code=404, detail=f"Order id {payload.order_id} not found.")
    else:
        order = await asyncio.to_thread(get_order_by_number, payload.voucher_number)
        if not order:
            raise HTTPException(status_code=404, detail=f"Order number '{payload.voucher_number}' not found.")

    voucher_number_for_erp = order.get("order_number") or payload.voucher_number

    try:
        erp_result = await asyncio.to_thread(
            push_manual_line_update,
            voucher_number=voucher_number_for_erp,
            article_number=payload.erp_article_no,
            unit_price=payload.unit_price,
            line_total=payload.total,
            discount_percent=payload.discount_percent,
            delivery_date=payload.delivery_date,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=422, detail=str(error))

    # ERP push succeeded. Now reflect it in the stored order row. If the DB
    # sync fails for any reason, the ERP has already been corrected — surface
    # the sync failure as a warning rather than losing that fact.
    try:
        await asyncio.to_thread(
            update_order_line_after_manual_correction,
            order["id"],
            payload.erp_article_no,
            unit_price=payload.unit_price,
            line_total=payload.total,
            discount_percent=payload.discount_percent,
            delivery_date=payload.delivery_date,
        )
    except Exception as error:
        erp_result["db_sync_warning"] = str(error)
        return erp_result

    erp_result["order_id"] = order["id"]
    return erp_result