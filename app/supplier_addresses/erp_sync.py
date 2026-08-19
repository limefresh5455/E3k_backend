import logging
from typing import Any

import requests

from app.config import ERP_BASE_URL, ERP_PASSWORD, ERP_USERNAME
from app.supplier_addresses.errors import ErpSupplierSyncError


logger = logging.getLogger(__name__)
ERP_ADDRESS_TIMEOUT_SECONDS = 120


def build_erp_address_payload(supplier: dict[str, Any]) -> dict[str, str]:
    """Map one locally stored supplier address to the ERP Address fields."""
    addresses = supplier.get("addresses") or []
    address = addresses[0] if addresses else {}

    def value(source: dict[str, Any], key: str) -> str:
        raw = source.get(key)
        return "" if raw is None else str(raw).strip()

    return {
        "F001": value(supplier, "supplier_number"),
        "F004": value(supplier, "name"),
        "F005": value(address, "department_attention"),
        "F006": value(address, "street"),
        "F008": value(address, "postal_code"),
        "F009": value(address, "city"),
        "F010": value(address, "country"),
        "F011": value(address, "business_phone"),
        "F012": value(address, "private_phone"),
        "F013": value(address, "fax"),
        "F015": value(address, "mobile"),
        "F016": value(supplier, "code_1"),
        "F017": value(supplier, "code_2"),
        "F018": value(supplier, "code_3"),
        "F019": value(supplier, "code_4"),
        "F060": value(address, "email"),
    }


def create_supplier_in_erp(supplier: dict[str, Any]) -> str:
    """Create a supplier once in ERP and return its ERP record id.

    This request is deliberately not retried: after a timeout, ERP may already
    have created the record, and an automatic retry could create a duplicate.
    """
    supplier_number = str(supplier.get("supplier_number") or "").strip()
    if not ERP_USERNAME or not ERP_PASSWORD:
        raise ErpSupplierSyncError(
            f"Supplier {supplier_number} was saved locally, but ERP credentials are not configured"
        )
    url = f"{ERP_BASE_URL.rstrip('/')}/api/Address/New"
    try:
        response = requests.post(
            url,
            json=build_erp_address_payload(supplier),
            headers={"accept": "application/json", "E3k-Api-Test": "true"},
            auth=(ERP_USERNAME, ERP_PASSWORD),
            timeout=ERP_ADDRESS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        record_id = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.exception("ERP supplier creation failed for supplier_number=%s", supplier_number)
        raise ErpSupplierSyncError(
            f"Supplier {supplier_number} was saved locally, but ERP synchronization failed"
        ) from exc

    if isinstance(record_id, bool) or not isinstance(record_id, (str, int)):
        normalized_record_id = ""
    else:
        normalized_record_id = str(record_id).strip()
    if not normalized_record_id.isdigit() or int(normalized_record_id) == 0:
        logger.error(
            "ERP rejected supplier creation for supplier_number=%s with record_id=%r",
            supplier_number,
            record_id,
        )
        raise ErpSupplierSyncError(
            f"Supplier {supplier_number} was saved locally, but ERP did not create the record"
        )
    return normalized_record_id
