import asyncio

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import LOCAL_PDF_MODE, OPENAI_API_KEY
from app.services.order_service import (
    get_pcloud_folders,
    is_already_processed,
    process_file,
    process_local_file,
    process_pdf_bytes,
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
