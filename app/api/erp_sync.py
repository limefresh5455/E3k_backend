from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services.erp_sync_job_service import get_sync_status, trigger_sync

router = APIRouter()


@router.post("/sync/trigger", tags=["erp-sync"])
def trigger_erp_sync():
    result = trigger_sync()
    if not result["started"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Sync already running.",
                "started_at": result.get("started_at"),
            },
        )

    return JSONResponse(
        status_code=202,
        content={
            "message": "Sync started in background.",
            "started_at": result["started_at"],
            "note": "Poll /sync/status to monitor progress.",
        },
    )


@router.get("/sync/status", tags=["erp-sync"])
def erp_sync_status():
    return get_sync_status()
