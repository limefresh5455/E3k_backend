import asyncio

from fastapi import APIRouter, HTTPException

from app.config import LOCAL_PDF_MODE, OPENAI_API_KEY
from app.services.order_service import (
    get_pcloud_folders,
    is_already_processed,
    process_file,
    process_local_file,
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
        files = get_local_pdfs()
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
    else:
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

