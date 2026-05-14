"""
erp_service.py
Handles all communication with the europa3000 Web API.

  1. Look up a supplier address number by company name  -> resolve_supplier_number()
  2. Convert extracted PDF data into ERP payload        -> build_erp_payload()
  3. Create a purchase order in the ERP                 -> create_purchase_order()
  4. One-shot helper that chains all three              -> push_to_erp()
"""

from datetime import datetime
from typing import Optional

import requests

from app.config import ERP_BASE_URL, ERP_PASSWORD, ERP_USERNAME


def _auth() -> tuple[str, str]:
    return (ERP_USERNAME, ERP_PASSWORD)


def _parse_date(date_str: Optional[str]) -> Optional[str]:
    """Convert DD.MM.YYYY -> ISO 8601 datetime (YYYY-MM-DDT00:00:00.000Z). Returns None on failure.
    Used for line-level DeliveryDate which the ERP expects as a full timestamp."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d.%m.%Y")
        return dt.strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return None


def _parse_date_only(date_str: Optional[str]) -> Optional[str]:
    """Convert DD.MM.YYYY -> date-only string (YYYY-MM-DD). Returns None on failure.
    Used for header-level VoucherDate which the ERP expects without a time component."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 1. Resolve supplier address number
# ---------------------------------------------------------------------------

def resolve_supplier_number(supplier_name: str) -> Optional[str]:
    """
    Search the ERP address master with a LIKE filter on the company name.
    Returns the F001 (address key) of the first match, or None.
    """
    search_value = f"%{supplier_name.strip()[:30]}%"

    payload = {
        "Fields": ["F001", "F004", "F008", "F009"],
        "Filters": [
            {
                "FieldNumber": 4,   # F004 = company/last name
                "Value": search_value,
                "Combine": 1,       # AndClampNo
                "Type": 3,          # Like
            }
        ],
        "Sortings": [],
    }

    response = requests.post(
        f"{ERP_BASE_URL}/api/Address/Custom",
        json=payload,
        auth=_auth(),
        timeout=30,
    )
    response.raise_for_status()
    results = response.json()

    if not results:
        return None

    return results[0].get("F001", "").strip() or None


# ---------------------------------------------------------------------------
# 2. Build ERP payload from extracted data
# ---------------------------------------------------------------------------

def build_erp_payload(extracted: dict, supplier_number: str) -> dict:
    """Convert LLM-extracted dict into the PurchaseOrder NewObject payload.

    Key rules that match the ERP's expected format:
    - VoucherDate  → date-only  "YYYY-MM-DD"  (no time component)
    - DeliveryDate → full ISO   "YYYY-MM-DDT00:00:00.000Z"  (per-line only)
    - Top-level DeliveryDate is NOT sent; the ERP derives it from line dates.
    - Lines carry SinglePrice (gross) + DiscountPercent separately.
      The ERP applies the discount itself; we must NOT pre-calculate net price.
    - VatCode is NOT included on lines (ERP rejects it at line level).
    """

    voucher_lines = []
    for line in extracted.get("VoucherLines", []):
        erp_line = {
            # Always CustomArticleVoucherLine — supplier article numbers are
            # not guaranteed to exist in the local article master.
            "$type": (
                "E3k.Web.Objects.DataTransfer.VoucherLines.CustomArticleVoucherLine,"
                " E3k.Web.Objects.DataTransfer"
            ),
            "Number": str(line.get("Number", "")).strip(),
            "Name": str(line.get("Description", "")).strip(),
            "Quantity": float(line.get("Quantity", 1)),
            # Gross (list) price — the ERP applies DiscountPercent itself.
            # Do NOT send a pre-calculated net price here.
            "SinglePrice": float(line.get("GrossPrice", line.get("Price", 0))),
        }

        # Only add DiscountPercent when the PDF actually shows a discount.
        if line.get("DiscountPercent") is not None:
            erp_line["DiscountPercent"] = float(line["DiscountPercent"])

        # Unit of measure (e.g. "M" for metres, "ST" for pieces).
        if line.get("DescriptionUnit"):
            erp_line["DescriptionUnit"] = str(line["DescriptionUnit"])

        # Per-line delivery date as full ISO timestamp.
        line_delivery = _parse_date(line.get("DeliveryDate"))
        if line_delivery:
            erp_line["DeliveryDate"] = line_delivery

        # VatCode intentionally omitted — the ERP does not accept it at line level.

        voucher_lines.append(erp_line)

    payload = {
        "CustomerNumber": supplier_number,
        "VoucherNumber": str(extracted.get("OurOrderNumber", "")).strip(),
        # VoucherDate must be date-only (YYYY-MM-DD), not a full timestamp.
        "VoucherDate": _parse_date_only(extracted.get("VoucherDate")),
        "VoucherLines": voucher_lines,
    }

    # Store supplier's own document/order number in ExternalNumber when present.
    external = extracted.get("SupplierVoucherNumber") or extracted.get("ExternalNumber")
    if external:
        payload["ExternalNumber"] = str(external).strip()

    # Top-level DeliveryDate is deliberately NOT added; the ERP derives it
    # from the earliest line-level DeliveryDate automatically.

    return payload


# ---------------------------------------------------------------------------
# 3. Create purchase order in ERP
# ---------------------------------------------------------------------------

def create_purchase_order(payload: dict) -> dict:
    """
    POST /api/PurchaseOrder/NewObject
    Returns { "record_id": "8661", "voucher_number": "2600364" } on success.
    Raises Exception on business errors.
    """
    response = requests.post(
        f"{ERP_BASE_URL}/api/PurchaseOrder/NewObject",
        json=payload,
        auth=_auth(),
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    # Success: ERP returns the Record ID as a plain JSON string e.g. "8661"
    if isinstance(body, str):
        return {
            "record_id": body,
            "voucher_number": payload.get("VoucherNumber", ""),
        }

    # Business error: ERP returns {"Message": "...", "Errors": [...]}
    if isinstance(body, dict) and "Message" in body:
        raise Exception(f"ERP error: {body.get('Message')} | {body.get('Errors', [])}")

    return {"raw": body, "voucher_number": payload.get("VoucherNumber", "")}


# ---------------------------------------------------------------------------
# 4. One-shot helper
# ---------------------------------------------------------------------------

def push_to_erp(extracted: dict) -> dict:
    """
    Full pipeline:
      1. Resolve supplier address number from extracted["Supplier"]
      2. Build ERP payload
      3. POST to PurchaseOrder/NewObject
      4. Return result dict

    Raises ValueError if supplier cannot be resolved.
    Raises Exception on ERP errors.
    """
    supplier_name = extracted.get("Supplier")

    # Guard: LLM returned null or a placeholder instead of a real supplier name
    if not supplier_name or not supplier_name.strip():
        raise ValueError(
            "Supplier name could not be extracted from the PDF. "
            "Check that the PDF is text-based and contains a clear company name in the header."
        )

    supplier_name = supplier_name.strip()
    supplier_number = resolve_supplier_number(supplier_name)
    if not supplier_number:
        raise ValueError(
            f"Supplier '{supplier_name}' was extracted from the PDF but was not found "
            f"in the ERP address master. Add the supplier to europa3000 first."
        )

    payload = build_erp_payload(extracted, supplier_number)
    result = create_purchase_order(payload)

    return {
        "erp_record_id": result.get("record_id"),
        "voucher_number": result.get("voucher_number"),
        "supplier_number": supplier_number,
        "supplier_name": supplier_name,
        "payload_sent": payload,
    }
