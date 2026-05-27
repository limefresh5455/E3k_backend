"""
erp_service.py
Updates existing purchase orders in europa3000 using extracted order-confirmation data.

Flow:
  1. Build ERP purchase-order voucher number from extracted OurOrderNumber (e.g. 2600718 -> B2600718)
  2. GET existing lines: /api/VoucherLine/{voucherNumber}
  3. Match extracted PDF lines to ERP lines
  4. PUT line updates to /api/VoucherLine/Update (one line at a time)
"""

from datetime import datetime
import logging
import math
import re
from typing import Optional

import requests

from app.config import ERP_BASE_URL, ERP_PASSWORD, ERP_USERNAME

logger = logging.getLogger("erp_service")


def _auth() -> tuple[str, str]:
    return (ERP_USERNAME, ERP_PASSWORD)


def _parse_date_for_update(date_str: Optional[str]) -> Optional[str]:
    """Convert DD.MM.YYYY -> 'YYYY-MM-DD 00:00:00.000' for T176.F035."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d 00:00:00.000")
    except ValueError:
        return None


def _parse_date_flexible(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    text = str(date_str).strip()
    formats = (
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _as_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace("'", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _truncate_decimals(value: float, decimals: int = 2) -> float:
    factor = 10 ** max(decimals, 0)
    return math.trunc(value * factor) / factor


def _effective_unit_price(pdf_line: dict) -> float:
    # Prefer explicit discounted/net price if available in extracted data.
    explicit = pdf_line.get("Price")
    if explicit is not None:
        return _as_float(explicit, default=0.0)

    gross = _as_float(pdf_line.get("GrossPrice", 0), default=0.0)
    discount = pdf_line.get("DiscountPercent")
    if discount is None:
        return gross
    discount_pct = _as_float(discount, default=0.0)
    return round(gross * (1 - (discount_pct / 100.0)), 2)


def _unit_factor(pdf_line: dict) -> float:
    """
    Some suppliers quote a price per pack/base unit (e.g. Einheit=100),
    while Quantity is in pack count. ERP expects price per single unit.
    """
    raw = (
        pdf_line.get("Einheit")
        or pdf_line.get("UnitFactor")
        or pdf_line.get("PriceUnit")
        or pdf_line.get("UnitSize")
        or pdf_line.get("DescriptionUnit")
    )
    factor = _as_float(raw, default=0.0)
    if factor > 0:
        return factor

    # Fallback: parse from free-text fields when extractor missed explicit column.
    text = " ".join(
        str(pdf_line.get(k, "") or "")
        for k in ("Description", "Name", "AdditionalText", "UnitText")
    )
    # Examples matched:
    # - "Einheit 100"
    # - "Preis pro 100"
    # - "/100"
    for pat in (
        r"\beinheit\s*[:=]?\s*(\d{1,4})\b",
        r"\bpreis\s*(?:pro|\/)\s*(\d{1,4})\b",
        r"/\s*(\d{1,4})\b",
    ):
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            parsed = _as_float(m.group(1), default=0.0)
            if parsed > 0:
                return parsed

    return 1.0


def _normalize_article(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _build_po_voucher_number(order_number: str) -> str:
    cleaned = (order_number or "").strip().upper()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("B") else f"B{cleaned}"


def _get_purchase_order_lines(voucher_number_b: str) -> list[dict]:
    response = requests.get(
        f"{ERP_BASE_URL}/api/VoucherLine/{voucher_number_b}",
        params={"type": "PurchaseOrder"},
        auth=_auth(),
        timeout=30,
    )
    if not response.ok:
        raise Exception(
            f"ERP VoucherLine GET failed ({response.status_code}): {response.text[:1500]}"
        )
    body = response.json()
    if not isinstance(body, list):
        raise Exception(f"ERP VoucherLine GET returned unexpected payload: {body}")
    return body


def _pick_best_erp_line(pdf_line: dict, erp_lines: list[dict], used_ids: set[int]) -> Optional[dict]:
    pdf_number = str(pdf_line.get("Number", "")).strip()
    pdf_norm = _normalize_article(pdf_number)
    pdf_desc = str(pdf_line.get("Description", "")).strip().lower()

    candidates = []
    for line in erp_lines:
        line_id = line.get("Id")
        if line_id in used_ids:
            continue

        article = str(line.get("ArticleNumber", "")).strip()
        if not article or not article.strip():
            continue

        line_flag = int(line.get("LineFlag", 0) or 0)
        # Only primary item lines; skip text continuation lines.
        if line_flag != 1:
            continue

        erp_norm = _normalize_article(article)
        erp_desc = str(line.get("Name", "")).strip().lower()

        score = 0
        if pdf_norm and erp_norm == pdf_norm:
            score += 200
        if pdf_norm and erp_norm.endswith(pdf_norm):
            score += 120
        if pdf_norm and pdf_norm.endswith(erp_norm):
            score += 100

        # Strip supplier prefix like "8590-" from ERP article number and compare again.
        erp_trimmed = _normalize_article(re.sub(r"^[A-Z0-9]+-", "", article.upper()))
        if pdf_norm and erp_trimmed == pdf_norm:
            score += 150
        if pdf_norm and erp_trimmed and pdf_norm.endswith(erp_trimmed):
            score += 80

        if pdf_desc and erp_desc:
            overlap = sum(1 for token in pdf_desc.split() if len(token) > 3 and token in erp_desc)
            score += min(overlap * 5, 40)

        if score > 0:
            candidates.append((score, line))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _update_voucher_line(
    *,
    voucher_number_b: str,
    erp_article_number: str,
    delivery_date: Optional[str],
    unit_price: float,
    line_total: float,
) -> str:
    payload = {
        "F002": voucher_number_b,
        "F003": erp_article_number,
        "F016": f"{_truncate_decimals(unit_price, 2):.2f}",
        "F018": f"{line_total:.2f}",
        # "F070": f"{unit_price:.2f}",
    }
    if delivery_date:
        payload["F035"] = delivery_date

    logger.info("ERP VoucherLine update payload: %s", payload)

    response = requests.put(
        f"{ERP_BASE_URL}/api/VoucherLine/Update",
        params={"type": "PurchaseOrder"},
        json=payload,
        auth=_auth(),
        timeout=30,
    )
    if not response.ok:
        raise Exception(
            f"ERP VoucherLine UPDATE failed ({response.status_code}) for article '{erp_article_number}': "
            f"{response.text[:1500]}"
        )

    body = response.json()
    if isinstance(body, (int, str)):
        return str(body)
    if isinstance(body, dict) and "Message" in body:
        raise Exception(f"ERP line update error: {body.get('Message')} | {body.get('Errors', [])}")
    return str(body)


def push_to_erp(extracted: dict) -> dict:
    """
    Update existing ERP purchase order lines (no new-object creation).
    Keeps return shape compatible with existing frontend expectations.
    """
    our_order_number = str(extracted.get("OurOrderNumber", "")).strip()
    if not our_order_number:
        raise ValueError("OurOrderNumber could not be extracted from the PDF.")

    voucher_number_b = _build_po_voucher_number(our_order_number)
    erp_lines = _get_purchase_order_lines(voucher_number_b)
    if not erp_lines:
        raise ValueError(f"No ERP lines found for purchase order '{voucher_number_b}'.")

    source_lines = extracted.get("VoucherLines", []) or []
    if not source_lines:
        raise ValueError("No voucher lines extracted from PDF; nothing to update in ERP.")

    used_ids: set[int] = set()
    updated_ids: list[str] = []
    updated_pdf_numbers: list[str] = []
    unit_factor_alert_lines: list[dict] = []
    long_delivery_alert_lines: list[dict] = []
    updated_count = 0
    order_date_dt = None
    order_date_raw = extracted.get("OrderDate") or extracted.get("VoucherDate")
    if order_date_raw:
        order_date_dt = _parse_date_flexible(order_date_raw)

    for pdf_line in source_lines:
        matched = _pick_best_erp_line(pdf_line, erp_lines, used_ids)
        if not matched:
            logger.warning(
                "No ERP voucher line match found for extracted line number=%s description=%s",
                pdf_line.get("Number"),
                pdf_line.get("Description"),
            )
            continue

        line_id = matched.get("Id")
        if isinstance(line_id, int):
            used_ids.add(line_id)

        erp_article_number = str(matched.get("ArticleNumber", "")).strip()
        logger.info(
            "Matched PDF line to ERP line: pdf_number=%s, pdf_description=%s, erp_article=%s, erp_id=%s",
            pdf_line.get("Number"),
            pdf_line.get("Description"),
            erp_article_number,
            matched.get("Id"),
        )
        delivery_date = _parse_date_for_update(pdf_line.get("DeliveryDate") or extracted.get("DeliveryDate"))
        raw_delivery = pdf_line.get("DeliveryDate") or extracted.get("DeliveryDate")
        if order_date_dt and raw_delivery:
            delivery_dt = _parse_date_flexible(raw_delivery)
            if delivery_dt and (delivery_dt - order_date_dt).days > 7:
                long_delivery_alert_lines.append(
                    {
                        "article_number": str(pdf_line.get("Number", "")).strip(),
                        "order_date": order_date_dt.strftime("%d.%m.%Y"),
                        "delivery_date": delivery_dt.strftime("%d.%m.%Y"),
                        "days_after_order": (delivery_dt - order_date_dt).days,
                    }
                )
        base_unit_price = _effective_unit_price(pdf_line)
        unit_factor = _unit_factor(pdf_line)
        unit_price = round(base_unit_price / unit_factor, 3)
        quantity = _as_float(pdf_line.get("Quantity", 0), default=0.0)
        line_total = round(quantity * base_unit_price, 2)
        if unit_factor != 1.0:
            unit_factor_alert_lines.append(
                {
                    "article_number": str(pdf_line.get("Number", "")).strip(),
                    "factor": unit_factor,
                    "base_unit_price": round(base_unit_price, 4),
                    "erp_unit_price": round(unit_price, 4),
                }
            )

        updated_id = _update_voucher_line(
            voucher_number_b=voucher_number_b,
            erp_article_number=erp_article_number,
            delivery_date=delivery_date,
            unit_price=unit_price,
            line_total=line_total,
        )
        updated_ids.append(updated_id)
        updated_pdf_numbers.append(str(pdf_line.get("Number", "")).strip().upper())
        updated_count += 1

    if updated_count == 0:
        raise ValueError(
            f"No ERP lines were updated for purchase order '{voucher_number_b}'. "
            "Check article-number mapping between PDF and ERP voucher lines."
        )

    # Keep existing response structure for frontend:
    # - erp_record_id: use first returned update id (e.g. "41965")
    # - voucher_number: keep original order number without forced 'B' prefix
    first_line = erp_lines[0] if erp_lines else {}
    supplier_number = str(first_line.get("VoucherAddress", "")).strip()
    supplier_name = extracted.get("Supplier", "")
    alerts: list[dict] = []
    if unit_factor_alert_lines:
        alerts.append(
            {
                "type": "unit_factor",
                "message": "Double-check required: Einheit/unit-factor pricing detected.",
                "lines": unit_factor_alert_lines,
            }
        )
    if long_delivery_alert_lines:
        alerts.append(
            {
                "type": "delivery_date_gt_one_week",
                "message": "Double-check required: Delivery date is more than one week after order date.",
                "lines": long_delivery_alert_lines,
            }
        )

    return {
        "erp_record_id": updated_ids[0],
        "voucher_number": our_order_number,
        "supplier_number": supplier_number,
        "supplier_name": supplier_name,
        "payload_sent": {
            "voucher_number_b": voucher_number_b,
            "updated_count": updated_count,
            "updated_ids": updated_ids,
            "updated_pdf_numbers": updated_pdf_numbers,
            "requires_double_check": bool(alerts),
            "alerts": alerts,
        },
    }
