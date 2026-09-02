from datetime import datetime
import logging
import math
import re
import time
from typing import Optional
from urllib.parse import quote

import requests

from app.config import ERP_BASE_URL, ERP_PASSWORD, ERP_USERNAME

logger = logging.getLogger("erp_service")

# ─────────────────────────────────────────────────
# ERP call resilience settings
# The ERP (teboag.ch) API endpoints can be slow/unresponsive under load.
# Instead of failing immediately after a single 30s timeout, we retry with
# increasing wait times, and honor Retry-After if the ERP ever sends one.
# ─────────────────────────────────────────────────
ERP_REQUEST_TIMEOUT = 120          # was 30 — give slow-but-working calls more room
ERP_MAX_RETRIES = 5                # total attempts per call
ERP_BASE_BACKOFF = 15               # seconds, doubles each retry
ERP_MAX_BACKOFF = 120                # cap the wait between retries


def _auth() -> tuple[str, str]:
    return (ERP_USERNAME, ERP_PASSWORD)


def _erp_backoff_seconds(attempt: int, response: Optional[requests.Response] = None) -> int:
    """Compute wait time before next retry, honoring Retry-After if present."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return int(float(retry_after))
            except ValueError:
                pass
    return min(ERP_BASE_BACKOFF * (2 ** attempt), ERP_MAX_BACKOFF)


def _erp_request(method: str, url: str, *, context: str, **kwargs) -> requests.Response:
    """
    Shared GET/PUT caller with timeout + retry/backoff for the ERP API.
    Retries on: connection errors, read timeouts, 429, and 5xx responses.
    Raises the final exception/response error if all retries are exhausted.
    """
    kwargs.setdefault("timeout", ERP_REQUEST_TIMEOUT)
    last_exc: Optional[Exception] = None

    for attempt in range(ERP_MAX_RETRIES):
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < ERP_MAX_RETRIES - 1:
                wait = _erp_backoff_seconds(attempt)
                logger.warning(
                    "ERP %s timeout/connection error on %s (attempt %d/%d): %s — retrying in %ds",
                    method, context, attempt + 1, ERP_MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
                continue
            logger.error(
                "ERP %s failed after %d attempts on %s: %s",
                method, ERP_MAX_RETRIES, context, exc,
            )
            raise Exception(
                f"ERP {method} request to '{context}' timed out after {ERP_MAX_RETRIES} attempts "
                f"({ERP_REQUEST_TIMEOUT}s each). The ERP server may be slow or unavailable. "
                f"Last error: {exc}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            if attempt < ERP_MAX_RETRIES - 1:
                wait = _erp_backoff_seconds(attempt, response)
                logger.warning(
                    "ERP %s got %d on %s (attempt %d/%d) — retrying in %ds",
                    method, response.status_code, context, attempt + 1, ERP_MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            raise Exception(
                f"ERP {method} request to '{context}' failed after {ERP_MAX_RETRIES} attempts "
                f"(last status {response.status_code}): {response.text[:1500]}"
            )

        return response

    # Should not be reached, but keeps type-checkers happy.
    if last_exc:
        raise last_exc
    raise Exception(f"ERP {method} request to '{context}' failed for an unknown reason.")


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
    # F016 must contain the gross/list "Preis" printed on the PDF. The ERP
    # applies the separately supplied discount (F017) itself.
    return _as_float(
        pdf_line.get("GrossPrice", pdf_line.get("Price", 0)),
        default=0.0,
    )

    # Previous logic derived the unit price from the printed line total. This
    # produced the discounted/net price in F016 (for example 42.70 / 2 = 21.35)
    # instead of the required gross price (64.70).
    # explicit = pdf_line.get("Price")
    # if explicit is not None:
    #     return _as_float(explicit, default=0.0)
    # qty = _as_float(pdf_line.get("Quantity", 0), default=0.0)
    # line_total_raw = (
    #     pdf_line.get("LineTotal")
    #     or pdf_line.get("Total")
    #     or pdf_line.get("Amount")
    #     or pdf_line.get("LineAmount")
    # )
    # if line_total_raw is not None and qty > 0:
    #     return round(_as_float(line_total_raw, default=0.0) / qty, 2)
    # return _as_float(pdf_line.get("GrossPrice", 0), default=0.0)


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
        explicit_total_num = round(_as_float(explicit_total, default=0.0), 2)

        # Some layouts (notably Nilfisk) print both the gross "Public price"
        # amount and a discounted "Subtotal" amount. If extraction selects the
        # gross amount as LineTotal, correct it only when the arithmetic clearly
        # proves that it is quantity x gross price. Already-discounted totals and
        # all non-discounted lines remain untouched.
        quantity = _as_float(pdf_line.get("Quantity", 0), default=0.0)
        discount_pct = _as_float(pdf_line.get("DiscountPercent"), default=0.0)
        if quantity > 0 and discount_pct > 0 and not has_surcharge_column:
            gross_total = round(quantity * unit_price, 2)
            discounted_total = round(gross_total * (1 - (discount_pct / 100.0)), 2)
            tolerance = max(0.02, abs(gross_total) * 0.001)
            if abs(explicit_total_num - gross_total) <= tolerance:
                return discounted_total, 0.0

        return explicit_total_num, 0.0

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


def _printed_line_total(pdf_line: dict) -> float:
    """
    Returns the line total as printed on the PDF (Betrag/Total/Amount), or 0.0
    if none was extracted for this line.
    """
    raw = (
        pdf_line.get("LineTotal")
        or pdf_line.get("Total")
        or pdf_line.get("Amount")
        or pdf_line.get("LineAmount")
    )
    return _as_float(raw, default=0.0)


def _resolve_unit_price_and_factor(pdf_line: dict, base_unit_price: float) -> tuple[float, float, str]:
    """
    Decide the correct unit price to send to the ERP, without trusting the
    extracted 'Einheit' value in isolation.

    Different suppliers use the 'Einheit' column for different things:
      - a genuine pricing factor (price is quoted per N pieces), where
        dividing base_unit_price by Einheit is correct, or
      - something else entirely (e.g. a repeated quantity, a pack-size
        descriptor unrelated to pricing) where dividing is WRONG and produces
        a unit price that is off by exactly that factor.

    Rather than guessing which case applies from the column label alone, we
    use the printed line total (Betrag) as ground truth: whichever
    interpretation (divided vs. not-divided) reproduces Menge x Preis ~= Betrag
    is the one we trust. This makes the check format-agnostic since the
    Menge x Preis = Betrag identity holds across supplier layouts, even when
    the meaning of 'Einheit' does not.

    Returns: (chosen_unit_price, effective_factor_used, resolution_reason)
    """
    unit_factor = _unit_factor(pdf_line)
    quantity = _as_float(pdf_line.get("Quantity", 0), default=0.0)
    line_total = _printed_line_total(pdf_line)

    candidate_divided = round(base_unit_price / unit_factor, 3) if unit_factor else round(base_unit_price, 3)
    candidate_plain = round(base_unit_price, 3)

    # No factor extracted (or factor is 1) -> nothing to reconcile.
    if unit_factor == 1.0:
        return candidate_plain, 1.0, "einheit_is_one"

    # Fast-path heuristic: Einheit exactly matches Quantity is a strong signal
    # that the column held a repeated/derived quantity, not a pricing factor
    # (this is the pattern seen with Festo AG confirmations).
    factor_equals_quantity = quantity > 0 and unit_factor == quantity

    if quantity > 0 and line_total > 0:
        error_divided = abs(candidate_divided * quantity - line_total)
        error_plain = abs(candidate_plain * quantity - line_total)
        # Small absolute tolerance for rounding noise in printed totals.
        tolerance = max(0.02, line_total * 0.01)

        if error_divided <= tolerance and error_divided <= error_plain:
            return candidate_divided, unit_factor, "confirmed_by_line_total"
        if error_plain <= tolerance and error_plain < error_divided:
            return candidate_plain, 1.0, "rejected_by_line_total"
        # Neither reproduces the printed total cleanly -> can't confirm either
        # way from arithmetic alone. Fall back to the quantity heuristic if it
        # applies, otherwise keep the extractor's literal answer but mark it
        # for review.
        if factor_equals_quantity:
            return candidate_plain, 1.0, "ambiguous_defaulted_by_quantity_match"
        return candidate_divided, unit_factor, "ambiguous_no_line_total_match"

    # No printed line total available to check against.
    if factor_equals_quantity:
        return candidate_plain, 1.0, "no_line_total_defaulted_by_quantity_match"
    return candidate_divided, unit_factor, "no_line_total_unverified"


def _normalize_article(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _build_po_voucher_number(order_number: str) -> str:
    cleaned = (order_number or "").strip().upper()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("B") else f"B{cleaned}"


def _get_purchase_order_lines(voucher_number_b: str) -> list[dict]:
    url = f"{ERP_BASE_URL}/api/VoucherLine/{voucher_number_b}"
    response = _erp_request(
        "GET",
        url,
        context=f"VoucherLine/{voucher_number_b}",
        params={"type": "PurchaseOrder"},
        auth=_auth(),
    )
    if not response.ok:
        raise Exception(
            f"ERP VoucherLine GET failed ({response.status_code}): {response.text[:1500]}"
        )
    body = response.json()
    if not isinstance(body, list):
        raise Exception(f"ERP VoucherLine GET returned unexpected payload: {body}")
    return body


def _lookup_supplier_article_f028(supplier_no: str, article_no: str) -> Optional[str]:
    """
    Call ERP POST /api/SupplierArticle/Custom to resolve the supplier's own
    article number (F028) for a given internal SupplierNumber + ArticleNumber
    combination.

    Request body shape (per ERP docs):
        {
            "Fields": ["F028"],
            "Filters": [
                {"FieldNumber": 1, "Value": <supplier_no>, "Combine": 1, "Type": 1},
                {"FieldNumber": 2, "Value": <article_no>,  "Combine": 1, "Type": 1}
            ]
        }

    Response shape:
        [ { "F028": "3600020L1DA05Z                                    " } ]

    Returns the normalized (regex-cleaned) F028 value, or None if the ERP
    call fails, returns no rows, or the row has no usable F028 value.
    Wrapped so any error is swallowed and behaves like "no match" — this is
    a best-effort fallback and must never break the existing matching flow.
    """
    try:
        url = f"{ERP_BASE_URL}/api/SupplierArticle/Custom"
        payload = {
            "Fields": ["F028"],
            "Filters": [
                {"FieldNumber": 1, "Value": supplier_no, "Combine": 1, "Type": 1},
                {"FieldNumber": 2, "Value": article_no, "Combine": 1, "Type": 1},
            ],
        }
        response = _erp_request(
            "POST",
            url,
            context="SupplierArticle/Custom",
            json=payload,
            auth=_auth(),
        )
        if not response.ok:
            logger.warning(
                "SupplierArticle/Custom lookup failed (%s) for supplier_no=%s, article_no=%s: %s",
                response.status_code, supplier_no, article_no, response.text[:500],
            )
            return None

        body = response.json()
        if not isinstance(body, list) or not body:
            return None

        raw_f028 = str(body[0].get("F028", "")).strip()
        if not raw_f028:
            return None

        return _normalize_article(raw_f028)
    except Exception as exc:
        logger.warning(
            "SupplierArticle/Custom lookup raised for supplier_no=%s, article_no=%s: %s",
            supplier_no, article_no, exc,
        )
        return None


def _pick_best_erp_line(
    pdf_line: dict,
    erp_lines: list[dict],
    used_ids: set[int],
    supplier_no: str,
) -> Optional[dict]:
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
        # ── Fallback: ERP SupplierArticle/Custom (F028) lookup ──
        # No normal-score candidate was found above. As a last resort, ask
        # the ERP directly (via SupplierArticle/Custom) for the supplier's
        # own article number (F028) for each candidate ERP line's
        # SupplierNumber + ArticleNumber, and compare that against the
        # article number printed on the PDF. This covers cases where the
        # supplier's article number is stored separately in the ERP's
        # SupplierArticle table (F028 field) rather than being derivable
        # from a simple string combination.
        # Wrapped in try/except so any error here behaves like "no match"
        # and never breaks the existing matching flow above.
        try:
            for line in erp_lines:
                line_id = line.get("Id")
                if line_id in used_ids:
                    continue

                line_flag = int(line.get("LineFlag", 0) or 0)
                if line_flag != 1:
                    continue

                article = str(line.get("ArticleNumber", "")).strip()
                if not article:
                    continue

                if not supplier_no:
                    continue

                f028_norm = _lookup_supplier_article_f028(supplier_no, article)
                if not f028_norm:
                    continue

                logger.info(
                    "F028 comparison: pdf_number=%s, pdf_norm=%s, supplier_no=%s, "
                    "erp_article=%s, f028_norm=%s, erp_id=%s",
                    pdf_number,
                    pdf_norm,
                    supplier_no,
                    article,
                    f028_norm,
                    line_id,
                )

                is_match = (
                    pdf_norm
                    and f028_norm
                    and (
                        f028_norm == pdf_norm
                        or f028_norm.startswith(pdf_norm)
                        or pdf_norm.startswith(f028_norm)
                    )
                )

                if is_match:
                    logger.info(
                        "Fallback SupplierArticle/Custom (F028) match found: "
                        "pdf_number=%s, supplier_no=%s, erp_article=%s, f028=%s, erp_id=%s",
                        pdf_number, supplier_no, article, f028_norm, line_id,
                    )
                    return line
        except Exception as exc:
            logger.warning(
                "Fallback SupplierArticle/Custom (F028) match check failed for pdf_number=%s: %s",
                pdf_number, exc,
            )
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

    url = f"{ERP_BASE_URL}/api/VoucherLine/Update"
    response = _erp_request(
        "PUT",
        url,
        context=f"VoucherLine/Update (article={erp_article_number})",
        params={"type": "PurchaseOrder"},
        json=payload,
        auth=_auth(),
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


def _update_article_sales_prices(
    *,
    article_number: str,
    sales_price_net: float,
    sales_price_gross: float,
) -> str:
    """Update the two sale-price fields on an ERP article master record."""
    article_number = str(article_number or "").strip()
    net = _as_float(sales_price_net, default=0.0)
    gross = _as_float(sales_price_gross, default=0.0)
    if not article_number:
        raise ValueError("ERP article number is required for the sales-price update.")
    if net <= 0 or gross <= 0:
        raise ValueError(
            f"Invalid MCC sales prices for article '{article_number}': net={net}, gross={gross}"
        )

    payload = {
        "F001": article_number,
        "F032": f"{net:.2f}",
        "F033": f"{gross:.2f}",
    }
    logger.info("ERP Article sales-price update payload: %s", payload)
    response = _erp_request(
        "PUT",
        f"{ERP_BASE_URL}/api/Article/Update",
        context=f"Article/Update sales prices (article={article_number})",
        json=payload,
        auth=_auth(),
    )
    if not response.ok:
        raise Exception(
            f"ERP Article UPDATE failed ({response.status_code}) for article "
            f"'{article_number}': {response.text[:1500]}"
        )

    body = response.json()
    if isinstance(body, dict) and "Message" in body:
        raise Exception(
            f"ERP article sales-price update error: {body.get('Message')} | "
            f"{body.get('Errors', [])}"
        )
    record_id = str(body).strip()
    if not record_id.isdigit() or int(record_id) <= 0:
        raise Exception(
            f"ERP returned an invalid sales-price update result for article "
            f"'{article_number}': {body!r}"
        )

    verify_response = _erp_request(
        "POST",
        f"{ERP_BASE_URL}/api/Article/Key/{quote(article_number, safe='')}",
        context=f"Article/Key sales-price verification (article={article_number})",
        json=["F032", "F033"],
        auth=_auth(),
    )
    if not verify_response.ok:
        raise Exception(
            f"ERP Article verification failed ({verify_response.status_code}) for "
            f"article '{article_number}': {verify_response.text[:1500]}"
        )
    saved = verify_response.json()
    if not isinstance(saved, dict):
        raise Exception(
            f"ERP returned an invalid sales-price verification result for article "
            f"'{article_number}': {saved!r}"
        )
    saved_net = _as_float(saved.get("F032"), default=float("nan"))
    saved_gross = _as_float(saved.get("F033"), default=float("nan"))
    if (
        not math.isfinite(saved_net)
        or not math.isfinite(saved_gross)
        or abs(saved_net - net) > 0.001
        or abs(saved_gross - gross) > 0.001
    ):
        raise Exception(
            f"ERP sales-price verification mismatch for article '{article_number}': "
            f"expected F032={net:.2f}, F033={gross:.2f}; "
            f"received F032={saved.get('F032')!r}, F033={saved.get('F033')!r}"
        )
    return record_id


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
        raise ValueError("voucher_number ist erforderlich.")

    article_number = str(article_number or "").strip()
    if not article_number:
        raise ValueError("article_number ist erforderlich.")

    formatted_delivery_date = None
    if delivery_date:
        parsed_date = _parse_date_flexible(str(delivery_date))
        if not parsed_date:
            raise ValueError(
                f"delivery_date '{delivery_date}' konnte nicht gelesen werden. "
                "Bitte DD.MM.YYYY oder YYYY-MM-DD verwenden."
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

    # SupplierArticle records are keyed by the supplier number stored on the
    # purchase-order address/header. Voucher lines do not necessarily repeat
    # that value themselves.
    supplier_no = str(erp_lines[0].get("VoucherAddress") or "").strip()

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
    unit_factor_corrected_lines: list[dict] = []
    long_delivery_alert_lines: list[dict] = []
    surcharge_alert_lines: list[dict] = []
    quantity_mismatch_alert_lines: list[dict] = []
    mcc_discount_alert_lines: list[dict] = []
    mcc_sales_price_validation_lines: list[dict] = []
    mcc_sales_price_error_lines: list[dict] = []
    mcc_sales_price_updates: dict[str, dict[str, float | str]] = {}
    updated_line_totals: dict[str, float] = {}
    updated_line_quantities: dict[str, float] = {}
    updated_line_erp_article_numbers: dict[str, str] = {}
    calculated_total = 0.0
    updated_count = 0
    is_mcc_order = bool(extracted.get("IsMccOrderConfirmation"))
    has_surcharge_column = bool(extracted.get("HasSurchargeColumn"))
    order_date_dt = None
    order_date_raw = extracted.get("OrderDate") or extracted.get("VoucherDate")
    if order_date_raw:
        order_date_dt = _parse_date_flexible(order_date_raw)

    for pdf_line in source_lines:
        matched = _pick_best_erp_line(pdf_line, erp_lines, used_ids, supplier_no)
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
        # Send the PDF's gross/list Preis directly to ERP field F016.
        unit_price = base_unit_price
        unit_factor = 1.0
        unit_price_resolution = "direct_gross_price"

        # Previous logic reconciled/divided the price using Einheit and the
        # printed line total before sending it to F016.
        # unit_price, unit_factor, unit_price_resolution = _resolve_unit_price_and_factor(
        #     pdf_line, base_unit_price
        # )
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
        if unit_price_resolution == "rejected_by_line_total":
            # Einheit was extracted as non-1, but dividing by it did NOT
            # reproduce the printed line total, while treating it as 1 did.
            # We auto-corrected; log it so the pattern can be tracked, but
            # this does not require manual review since the total confirms it.
            unit_factor_corrected_lines.append(
                {
                    "article_number": str(pdf_line.get("Number", "")).strip(),
                    "extracted_einheit": _unit_factor(pdf_line),
                    "base_unit_price": round(base_unit_price, 4),
                    "corrected_unit_price": round(unit_price, 4),
                    "reason": unit_price_resolution,
                }
            )
        elif unit_price_resolution in (
            "ambiguous_no_line_total_match",
            "ambiguous_defaulted_by_quantity_match",
            "no_line_total_unverified",
            "no_line_total_defaulted_by_quantity_match",
        ):
            # Either the printed total didn't clearly confirm either
            # interpretation, or there was no total to check against at all.
            # These genuinely need a human to double-check.
            unit_factor_alert_lines.append(
                {
                    "article_number": str(pdf_line.get("Number", "")).strip(),
                    "factor": unit_factor,
                    "base_unit_price": round(base_unit_price, 4),
                    "erp_unit_price": round(unit_price, 4),
                    "reason": unit_price_resolution,
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
        mcc_sales_price_net = pdf_line.get("MccSalesPriceNet")
        mcc_sales_price_gross = pdf_line.get("MccSalesPriceGross")
        mcc_sales_price_excluded = bool(pdf_line.get("MccSalesPriceExcluded"))
        if pdf_line.get("MccDiscountDiffersFromDefault"):
            mcc_discount_alert_lines.append(
                {
                    "article_number": erp_article_number,
                    "discount_percent": _as_float(pdf_line.get("DiscountPercent")),
                    "default_discount_percent": 55.0,
                }
            )
        if (
            is_mcc_order
            and not mcc_sales_price_excluded
            and mcc_sales_price_net is not None
            and mcc_sales_price_gross is not None
        ):
            proposed_prices = {
                "net": round(_as_float(mcc_sales_price_net), 2),
                "gross": round(_as_float(mcc_sales_price_gross), 2),
            }
            previous_update = mcc_sales_price_updates.get(erp_article_number)
            if previous_update is None:
                try:
                    article_record_id = _update_article_sales_prices(
                        article_number=erp_article_number,
                        sales_price_net=proposed_prices["net"],
                        sales_price_gross=proposed_prices["gross"],
                    )
                    mcc_sales_price_updates[erp_article_number] = {
                        **proposed_prices,
                        "erp_record_id": article_record_id,
                    }
                except Exception as exc:
                    logger.exception(
                        "MCC sales-price update failed for ERP article=%s",
                        erp_article_number,
                    )
                    mcc_sales_price_error_lines.append(
                        {
                            "article_number": erp_article_number,
                            **proposed_prices,
                            "error": str(exc),
                        }
                    )
            elif (
                previous_update.get("net") != proposed_prices["net"]
                or previous_update.get("gross") != proposed_prices["gross"]
            ):
                mcc_sales_price_error_lines.append(
                    {
                        "article_number": erp_article_number,
                        **proposed_prices,
                        "error": "Conflicting MCC sales prices for the same article in one order.",
                    }
                )
        elif is_mcc_order and not mcc_sales_price_excluded:
            mcc_sales_price_validation_lines.append(
                {
                    "article_number": erp_article_number,
                    "error": (
                        "MCC gross price, discount, printed net price, or line total "
                        "did not pass the sales-price cross-check."
                    ),
                }
            )
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
    if mcc_discount_alert_lines:
        alerts.append(
            {
                "type": "mcc_discount_differs_from_default",
                "message": "Prüfung erforderlich: Der MCC-Rabatt weicht vom Standardwert 55 % ab; für die Verkaufspreise wurde der im Dokument angegebene Rabatt verwendet.",
                "lines": mcc_discount_alert_lines,
            }
        )
    if mcc_sales_price_error_lines:
        alerts.append(
            {
                "type": "mcc_sales_price_update_failed",
                "message": "Prüfung erforderlich: Mindestens ein berechneter MCC-Verkaufspreis konnte nicht im ERP-Artikel gespeichert werden.",
                "lines": mcc_sales_price_error_lines,
            }
        )
    if mcc_sales_price_validation_lines:
        alerts.append(
            {
                "type": "mcc_sales_price_validation_failed",
                "message": "Prüfung erforderlich: Mindestens eine MCC-Zeile konnte nicht sicher für die Verkaufspreise validiert werden.",
                "lines": mcc_sales_price_validation_lines,
            }
        )

    if updated_count < expected_line_count:
        alerts.append(
            {
                "type": "fewer_lines_than_expected",
                "message": (
                    "Prüfung erforderlich: Es wurden weniger PDF-Zeilen abgeglichen/aktualisiert, "
                    "als die ERP-Bestellung tatsächlich enthält - möglicherweise fehlen einige Zeilen."
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
        pct = round((diff / calculated_total) * 100.0, 2) if calculated_total > 0 else None
        # Deviations within 10% of the calculated line total are treated as acceptable
        # and are not flagged. Anything at/above 10% (or where a percentage can't be
        # computed, e.g. calculated_total == 0) still requires review.
        within_tolerance = pct is not None and abs(pct) < 10.0
        if abs(diff) >= 0.05 and not within_tolerance:
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
            "mcc_sales_price_updates": mcc_sales_price_updates,
            "requires_double_check": bool(alerts),
            "alerts": alerts,
        },
    }
