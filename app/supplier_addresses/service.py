from typing import Optional

from app.supplier_addresses import repository
from app.supplier_addresses.erp_sync import create_supplier_in_erp
from app.supplier_addresses.excel_export import (
    append_supplier_to_master_workbook,
    append_suppliers_to_master_workbook,
)
from app.supplier_addresses.errors import ErpSupplierSyncError, SupplierNotFoundError
from app.supplier_addresses.schemas import SupplierCreate, SupplierUpdate


def list_suppliers(search: Optional[str]) -> dict:
    clean_search = search.strip() if search else None
    return repository.list_suppliers(clean_search or None)


def get_supplier(supplier_number: str) -> dict:
    supplier = repository.get_supplier(supplier_number)
    if supplier is None:
        raise SupplierNotFoundError("Supplier not found")
    return supplier


def create_supplier(payload: SupplierCreate) -> dict:
    supplier = repository.create_supplier(payload.model_dump())
    create_supplier_in_erp(supplier)
    append_supplier_to_master_workbook(supplier)
    supplier["message"] = (
        "Supplier added successfully, synchronized with ERP, and appended to Excel."
    )
    supplier["erp_synced"] = True
    return supplier


def update_supplier(supplier_number: str, payload: SupplierUpdate) -> dict:
    changes = payload.model_dump(exclude_unset=True)
    return repository.update_supplier(supplier_number, changes)


def upload_suppliers(records: list[dict], rows_read: int) -> dict:
    result = repository.import_suppliers(records)
    added = result["imported"]
    inserted_numbers = set(result["inserted_supplier_numbers"])
    inserted_records = [
        record for record in records if record["supplier_number"] in inserted_numbers
    ]
    erp_synced = 0
    erp_failed_supplier_numbers = []
    for record in inserted_records:
        try:
            create_supplier_in_erp(record)
            erp_synced += 1
        except ErpSupplierSyncError:
            # Each Excel row is independent. Keep processing and report all
            # suppliers which were saved locally but not confirmed by ERP.
            erp_failed_supplier_numbers.append(record["supplier_number"])
    excel_result = append_suppliers_to_master_workbook(inserted_records)
    failed = len(erp_failed_supplier_numbers)
    if added == 0:
        message = (
            "Your supplier list is already up to date. "
            "ERP synchronization was not performed."
        )
    elif failed:
        added_noun = "supplier" if added == 1 else "suppliers"
        failed_noun = "supplier" if failed == 1 else "suppliers"
        message = (
            f"{added} new {added_noun} added locally; "
            f"{failed} {failed_noun} failed ERP synchronization."
        )
    else:
        noun = "supplier" if added == 1 else "suppliers"
        message = f"{added} new {noun} added and synchronized successfully."
    return {
        "message": message,
        "rows_read": rows_read,
        "suppliers_in_file": len(records),
        "suppliers_added": added,
        "suppliers_already_present": result["skipped_existing"],
        "addresses_added": result["addresses_imported"],
        "erp_synced": erp_synced,
        "erp_failed": failed,
        "erp_failed_supplier_numbers": erp_failed_supplier_numbers,
        "excel_appended": excel_result["appended"],
        "excel_already_present": excel_result["already_present"],
    }
