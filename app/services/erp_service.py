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
    # 1) Prefer explicit extracted unit price from the PDF.
    explicit = pdf_line.get("Price")
    if explicit is not None:
        return _as_float(explicit, default=0.0)

    # 2) If line total is available, derive exact unit price from it.
    qty = _as_float(pdf_line.get("Quantity", 0), default=0.0)
    line_total_raw = (
        pdf_line.get("LineTotal")
        or pdf_line.get("Total")
        or pdf_line.get("Amount")
        or pdf_line.get("LineAmount")
    )
    if line_total_raw is not None and qty > 0:
        return round(_as_float(line_total_raw, default=0.0) / qty, 2)

    # 3) Final fallback: use listed unit price directly from PDF (no percentage math).
    return _as_float(pdf_line.get("GrossPrice", 0), default=0.0)


def _resolve_surcharge_percent(pdf_line: dict, has_surcharge_column: bool = False) -> float:
    surcharge_pct_raw = (
        pdf_line.get("SurchargePercent")
        or pdf_line.get("AufschlagPercent")
    )
    if surcharge_pct_raw is None:
        row_text = " ".join(
            str(pdf_line.get(k, "") or "")
            for k in ("Description", "Name", "AdditionalText")
        ).lower()
        if "aufschlag" in row_text or "surcharge" in row_text:
            surcharge_pct_raw = pdf_line.get("DiscountPercent")
    if surcharge_pct_raw is None and has_surcharge_column:
        surcharge_pct_raw = pdf_line.get("DiscountPercent")
    return _as_float(surcharge_pct_raw, default=0.0)


def _effective_line_total(pdf_line: dict, unit_price: float, has_surcharge_column: bool = False) -> tuple[float, float]:
    # 1) Prefer explicit line total from extracted PDF columns.
    explicit_total = (
        pdf_line.get("LineTotal")
        or pdf_line.get("Total")
        or pdf_line.get("Amount")
        or pdf_line.get("LineAmount")
    )
    if explicit_total is not None:
        return round(_as_float(explicit_total, default=0.0), 2), 0.0

    # 2) Fallback to qty * unit price.
    quantity = _as_float(pdf_line.get("Quantity", 0), default=0.0)
    base_total = round(quantity * unit_price, 2)

    # 3) If surcharge is present (e.g. "Aufschlag 5.5%"), add it to total.
    surcharge_pct = _resolve_surcharge_percent(pdf_line, has_surcharge_column=has_surcharge_column)
    if surcharge_pct > 0:
        return round(base_total * (1 + (surcharge_pct / 100.0)), 2), surcharge_pct

    # 4) If discount is present, apply it to total.
    discount_pct = _as_float(pdf_line.get("DiscountPercent"), default=0.0)
    if discount_pct > 0:
        return round(base_total * (1 - (discount_pct / 100.0)), 2), 0.0

    return base_total, 0.0


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

        # Require a real article-number correspondence to count as a match.
        # Description-overlap alone maxes out at 40, so this floor rejects
        # "matches" based purely on a coincidental shared word, which previously
        # let genuinely-unmatched PDFs sneak past the zero-match failure check
        # below and get flagged as attention instead of failure.
        _MIN_MATCH_SCORE = 50
        if score >= _MIN_MATCH_SCORE:
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
    discount_percent: Optional[float] = None,
) -> str:
    payload = {
        "F002": voucher_number_b,
        "F003": erp_article_number,
        "F016": f"{_truncate_decimals(unit_price, 2):.2f}",
        "F018": f"{line_total:.2f}",
        # "F070": f"{unit_price:.2f}",
    }
    if discount_percent is not None:
        payload["F017"] = f"{_as_float(discount_percent):.2f}"
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


def push_manual_line_update(
    *,
    voucher_number: str,
    article_number: str,
    unit_price: float,
    line_total: float,
    discount_percent: Optional[float] = None,
    delivery_date: Optional[str] = None,
) -> dict:
    """
    Manual dashboard correction: user supplies order/voucher number, article
    number, unit price, total, discount, and delivery date directly, and we
    push exactly those values to the ERP. No PDF extraction, no line
    matching, no mismatch checks — the user is the source of truth here.

    voucher_number: e.g. "2601018" - the "B" prefix is added automatically
    (same convention as the PDF pipeline), so callers should NOT prefix it themselves.
    delivery_date: accepts "DD.MM.YYYY" or "YYYY-MM-DD" (or other formats
    handled by _parse_date_flexible). Omit if not changing it.
    """
    voucher_number_b = _build_po_voucher_number(voucher_number)
    if not voucher_number_b:
        raise ValueError("voucher_number is required.")

    article_number = str(article_number or "").strip()
    if not article_number:
        raise ValueError("article_number is required.")

    formatted_delivery_date = None
    if delivery_date:
        parsed_date = _parse_date_flexible(str(delivery_date))
        if not parsed_date:
            raise ValueError(
                f"Could not parse delivery_date '{delivery_date}'. Use DD.MM.YYYY or YYYY-MM-DD."
            )
        formatted_delivery_date = parsed_date.strftime("%Y-%m-%d 00:00:00.000")

    unit_price_f = _as_float(unit_price)
    line_total_f = _as_float(line_total)

    erp_line_id = _update_voucher_line(
        voucher_number_b=voucher_number_b,
        erp_article_number=article_number,
        delivery_date=formatted_delivery_date,
        unit_price=unit_price_f,
        line_total=line_total_f,
        discount_percent=discount_percent,
    )

    logger.info(
        "Manual ERP line update pushed: voucher=%s, article=%s, unit_price=%s, total=%s, discount=%s, delivery_date=%s, erp_line_id=%s",
        voucher_number_b,
        article_number,
        unit_price_f,
        line_total_f,
        discount_percent,
        formatted_delivery_date,
        erp_line_id,
    )

    return {
        "status": "success",
        "erp_line_id": erp_line_id,
        "voucher_number": voucher_number_b,
        "article_number": article_number,
        "unit_price": round(_truncate_decimals(unit_price_f, 2), 2),
        "line_total": round(line_total_f, 2),
        "discount_percent": discount_percent,
        "delivery_date": formatted_delivery_date,
    }


def push_to_erp(extracted: dict) -> dict:
    """
    Update existing ERP purchase order lines (no new-object creation).
    Keeps return shape compatible with existing frontend expectations.
    """
    our_order_number = str(extracted.get("OurOrderNumber", "")).strip()
    if not our_order_number:
        raise ValueError("OurOrderNumber konnte nicht aus dem PDF extrahiert werden.")

    voucher_number_b = _build_po_voucher_number(our_order_number)
    erp_lines = _get_purchase_order_lines(voucher_number_b)
    if not erp_lines:
        raise ValueError(f"Keine ERP-Zeilen für den Auftrag '{voucher_number_b}' gefunden.")

    expected_line_count = sum(
        1
        for line in erp_lines
        if int(line.get("LineFlag", 0) or 0) == 1 and str(line.get("ArticleNumber", "")).strip()
    )

    source_lines = extracted.get("VoucherLines", []) or []
    if not source_lines:
        raise ValueError("Keine Belegzeilen aus dem PDF extrahiert; nichts im ERP zu aktualisieren.")

    used_ids: set[int] = set()
    updated_ids: list[str] = []
    updated_pdf_numbers: list[str] = []
    unit_factor_alert_lines: list[dict] = []
    long_delivery_alert_lines: list[dict] = []
    surcharge_alert_lines: list[dict] = []
    quantity_mismatch_alert_lines: list[dict] = []
    updated_line_totals: dict[str, float] = {}
    updated_line_quantities: dict[str, float] = {}
    updated_line_erp_article_numbers: dict[str, str] = {}
    calculated_total = 0.0
    updated_count = 0
    has_surcharge_column = bool(extracted.get("HasSurchargeColumn"))
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
        erp_quantity = _as_float(matched.get("Quantity", 0), default=0.0)
        extracted_quantity = _as_float(pdf_line.get("Quantity", 0), default=0.0)
        qty_for_total = erp_quantity if erp_quantity > 0 else extracted_quantity

        # Build a temporary line view so total logic can use stable quantity from ERP.
        pdf_line_for_total = dict(pdf_line)
        pdf_line_for_total["Quantity"] = qty_for_total
        line_total, surcharge_pct_applied = _effective_line_total(
            pdf_line_for_total,
            base_unit_price,
            has_surcharge_column=has_surcharge_column,
        )
        pdf_num = str(pdf_line.get("Number", "")).strip().upper()
        if pdf_num:
            updated_line_totals[pdf_num] = line_total
            if qty_for_total > 0:
                updated_line_quantities[pdf_num] = qty_for_total
            updated_line_erp_article_numbers[pdf_num] = erp_article_number
        calculated_total += line_total

        if erp_quantity > 0 and extracted_quantity > 0 and abs(extracted_quantity - erp_quantity) >= 1:
            quantity_mismatch_alert_lines.append(
                {
                    "article_number": str(pdf_line.get("Number", "")).strip(),
                    "extracted_quantity": extracted_quantity,
                    "erp_quantity": erp_quantity,
                }
            )
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
            discount_percent=pdf_line.get("DiscountPercent"),
        )
        updated_ids.append(updated_id)
        updated_pdf_numbers.append(pdf_num)
        if surcharge_pct_applied > 0:
            surcharge_alert_lines.append(
                {
                    "article_number": str(pdf_line.get("Number", "")).strip(),
                    "surcharge_percent": round(surcharge_pct_applied, 2),
                    "line_total_after_surcharge": round(line_total, 2),
                }
            )
        updated_count += 1

    if updated_count == 0:
        raise ValueError(
            f"Für den Auftrag '{voucher_number_b}' wurden keine ERP-Zeilen aktualisiert. "
            "Bitte die Artikelnummer-Zuordnung zwischen PDF und ERP-Belegzeilen prüfen."
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
<<<<<<< HEAD
                "message": "Double-check required: Einheit/unit-factor pricing detected.",
                "lines": unit_factor_alert_lines,
            }
        )
=======
                "message": "Prüfung erforderlich: Einheit/Preisfaktor konnte anhand des gedruckten Zeilen-Gesamtbetrags nicht bestätigt werden.",
                "lines": unit_factor_alert_lines,
            }
        )
    if unit_factor_corrected_lines:
        alerts.append(
            {
                "type": "unit_factor_auto_corrected",
                "message": "Nur zur Info: Der extrahierte Einheit-Wert stimmte nicht mit dem gedruckten Zeilen-Gesamtbetrag überein und wurde ignoriert; es wurde der gedruckte Preis verwendet.",
                "lines": unit_factor_corrected_lines,
            }
        )
>>>>>>> 039e881 (Translate ERP and order service user-facing messages to German)
    if long_delivery_alert_lines:
        alerts.append(
            {
                "type": "delivery_date_gt_one_week",
                "message": "Prüfung erforderlich: Das Lieferdatum liegt mehr als eine Woche nach dem Bestelldatum.",
                "lines": long_delivery_alert_lines,
            }
        )
    if surcharge_alert_lines:
        alerts.append(
            {
                "type": "surcharge_added",
                "message": "Prüfung erforderlich: Zusätzlicher Aufschlag hinzugefügt (z. B. 5,5 %).",
                "lines": surcharge_alert_lines,
            }
        )
    if quantity_mismatch_alert_lines:
        alerts.append(
            {
                "type": "quantity_mismatch",
                "message": "Prüfung erforderlich: Die Menge im PDF weicht von der ERP-Bestellmenge ab. Für die Summen wurde die ERP-Menge verwendet.",
                "lines": quantity_mismatch_alert_lines,
            }
        )

    recovered_line_count = int(extracted.get("RecoveredLineCount", 0) or 0)
    if recovered_line_count > 0:
        alerts.append(
            {
                "type": "lines_recovered_via_fallback",
                "message": (
                    "Double-check required: initial extraction missed line(s) that a fallback "
                    "text-table parser had to recover."
                ),
                "lines": [{"recovered_line_count": recovered_line_count}],
            }
        )

    if updated_count < expected_line_count:
        alerts.append(
            {
                "type": "fewer_lines_than_expected",
                "message": (
                    "Double-check required: fewer PDF lines were matched/updated than the "
                    "ERP purchase order actually has - some rows may be missing."
                ),
                "lines": [
                    {
                        "updated_line_count": updated_count,
                        "expected_line_count": expected_line_count,
                        "extracted_line_count": len(source_lines),
                    }
                ],
            }
        )

    pdf_total = extracted.get("TotalNetFromPdf")
    pdf_total_num = _as_float(pdf_total, default=0.0) if pdf_total is not None else 0.0
    if pdf_total_num > 0:
        diff = round(pdf_total_num - calculated_total, 2)
        if abs(diff) >= 0.05:
            pct = round((diff / calculated_total) * 100.0, 2) if calculated_total > 0 else None
            msg = "Prüfung erforderlich: Der PDF-Gesamtbetrag weicht vom aktualisierten Zeilen-Gesamtbetrag ab."
            if pct is not None and abs(pct - 5.5) <= 0.25:
                msg = (
                    "Prüfung erforderlich: Zusätzlicher Aufschlag hinzugefügt (ca. 5,5 %), "
                    "trotzdem bitte prüfen."
                )
            alerts.append(
                {
                    "type": "pdf_total_mismatch",
                    "message": msg,
                    "lines": [
                        {
                            "pdf_total": round(pdf_total_num, 2),
                            "calculated_line_total": round(calculated_total, 2),
                            "difference": diff,
                            "difference_percent": pct,
                        }
                    ],
                }
            )

    return {
        "erp_record_id": updated_ids[0],
        "voucher_number": our_order_number,
        "supplier_number": supplier_number,
        "supplier_name": supplier_name,
        "erp_article_numbers": ", ".join(dict.fromkeys(updated_line_erp_article_numbers.values())),
        "payload_sent": {
            "voucher_number_b": voucher_number_b,
            "updated_count": updated_count,
            "updated_ids": updated_ids,
            "updated_pdf_numbers": updated_pdf_numbers,
            "updated_line_totals": updated_line_totals,
            "updated_line_quantities": updated_line_quantities,
            "updated_line_erp_article_numbers": updated_line_erp_article_numbers,
            "requires_double_check": bool(alerts),
            "alerts": alerts,
        },
    }