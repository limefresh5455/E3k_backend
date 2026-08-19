from typing import Optional
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from openpyxl.utils.exceptions import InvalidFileException

from app.supplier_addresses import service
from app.supplier_addresses.errors import (
    AddressNotFoundError,
    ErpSupplierSyncError,
    SupplierConflictError,
    SupplierNotFoundError,
    SupplierWorkbookError,
)
from app.supplier_addresses.excel_export import get_master_workbook_path
from app.supplier_addresses.excel_import import parse_supplier_master_upload
from app.supplier_addresses.schemas import (
    SupplierCreate,
    SupplierCreateResponse,
    SupplierListResponse,
    SupplierResponse,
    SupplierUpdate,
    SupplierUploadResponse,
)

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.get("", response_model=SupplierListResponse)
def list_suppliers_endpoint(
    search: Optional[str] = Query(None, max_length=255),
):
    return service.list_suppliers(search)


@router.post("/upload", response_model=SupplierUploadResponse)
async def upload_suppliers_endpoint(file: UploadFile = File(...)):
    """Import an .xlsx address-list workbook in the canonical four-row format."""
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Only .xlsx files are supported")

    try:
        size = await run_in_threadpool(_upload_size, file)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Excel file must not exceed 50 MB")
        if size == 0:
            raise HTTPException(
                status_code=400, detail="Please upload data into the file and re-upload"
            )
        records, rows_read = await run_in_threadpool(parse_supplier_master_upload, file.file)
    except HTTPException:
        raise
    except (BadZipFile, InvalidFileException, OSError, ParseError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid Excel file") from exc
    finally:
        await file.close()

    try:
        return await run_in_threadpool(service.upload_suppliers, records, rows_read)
    except SupplierWorkbookError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _upload_size(file: UploadFile) -> int:
    current = file.file.tell()
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(current)
    return size


@router.get("/download", response_class=FileResponse)
def download_suppliers_endpoint():
    try:
        workbook_path = get_master_workbook_path()
        return FileResponse(
            path=workbook_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=workbook_path.name,
        )
    except SupplierWorkbookError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{supplier_number}", response_model=SupplierResponse)
def get_supplier_endpoint(supplier_number: str):
    try:
        return service.get_supplier(supplier_number)
    except SupplierNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("", response_model=SupplierCreateResponse, status_code=status.HTTP_201_CREATED)
def create_supplier_endpoint(payload: SupplierCreate):
    try:
        return service.create_supplier(payload)
    except SupplierConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ErpSupplierSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SupplierWorkbookError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/{supplier_number}", response_model=SupplierResponse)
def update_supplier_endpoint(
    supplier_number: str,
    payload: SupplierUpdate,
):
    try:
        return service.update_supplier(supplier_number, payload)
    except SupplierNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AddressNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SupplierConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
