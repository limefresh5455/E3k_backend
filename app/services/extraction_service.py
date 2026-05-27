import io
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta

import fitz  # PyMuPDF
import pdfplumber
import pytesseract
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from PIL import Image

from app.config import OPENAI_API_KEY, TESSERACT_CMD

logger = logging.getLogger("extraction_service")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    logger.info("Using Tesseract binary at: %s", TESSERACT_CMD)

# Placeholder values the LLM sometimes copies literally from the prompt template.
# If the extracted value matches any of these, treat it as not extracted.
_PLACEHOLDER_VALUES = {
    "supplier company name",
    "2600xxx",
    "article number",
    "product description",
    "dd.mm.yyyy",
    "chf or eur",
    "customer number or null",
}

_RECIPIENT_BLOCKLIST = {
    "schlauchservice baumann gmbh",
    "schlauch-service baumann gmbh",
    "schlauch service baumann gmbh",
}

_COMPANY_SUFFIX_PATTERN = r"(?:AG|GMBH|SARL|SAS|SA|SRL|KG|OHG|LIMITED|LTD|INC|BV|NV)\b"
_GENERIC_NON_SUPPLIER_WORDS = {
    "betrag",
    "total",
    "summe",
    "mwst",
    "ust",
    "rabatt",
    "rechnung",
    "lieferschein",
    "auftragsbestatigung",
    "auftragsbestätigung",
    "bestellung",
    "datum",
    "kundennummer",
    "artikel",
}
_GENERIC_NON_SUPPLIER_PARTS = (
    "auftrag",
    "bestellung",
    "lieferschein",
    "rechnung",
    "datum",
    "betrag",
    "summe",
    "kundennummer",
    "kunden-nr",
    "liefertermin",
    "versand",
    "artikel",
    "ihren",
)

SYSTEM_PROMPT = """You are a precise data extraction assistant for a Swiss hose service company (Schlauchservice Baumann GmbH).
You receive raw text extracted from supplier order confirmation PDFs (in German) and must extract structured order data.

IMPORTANT RULES:
- "Supplier": the SENDER of the document — the company that issued this order confirmation or invoice.
  The recipient is always Schlauchservice Baumann GmbH — never use that as the supplier.
  Search these locations IN ORDER until you find a name:
    1. An "=== IMAGE TEXT (OCR) ===" section appended after the main PDF text — this contains
       text recovered from raster images such as logos and footer bars. Company names, addresses,
       email addresses (e.g. info@cleanfix.com) and website URLs (e.g. www.cleanfix.com) found
       here are strong signals for the supplier name. Extract the company name from the footer
       contact block (e.g. "Cleanfix Reinigungssysteme AG | Stettenstrasse …").
    2. Letterhead, header area, or "Von:" / "Absender" field in the main text.
    3. Email domain or website domain in the footer (e.g. info@firma.ch → supplier is Firma AG/GmbH,
       www.supplier.com → supplier is Supplier).
  NEVER use placeholder text. If you cannot find the supplier name after checking all of the above, set it to null.
- "OurOrderNumber" is the BUYER's order number - look for patterns like 2600xxx, BEST. 2600xxx, "Ihre Bestellung", "I/Bestellung", "Ihre Bestellnr.", "Ihr Auftrag", "Bestellreferenz"
- "CustomerNumber" is the supplier's customer number for Schlauchservice Baumann - look for "Kunden-Nr.", "Debitorennr.", "Kundennr.", "Kunden NR.", "Ihre Kunden-Nr.", "Kundennummer"
- All dates in the output must use the format DD.MM.YYYY (e.g. 18.03.2026). Convert ALL date formats to this.
- "DeliveryDate": the confirmed delivery/dispatch date - look for "Lieferung/Termin", "Auslieferdatum", "Versandtermin", "Lieferung", "Termin best.", "Warenausgangsdatum", "Versand-Datum", "Liefertermin"
  * If the delivery date is given as a calendar week like "KW 11" or "KW11", convert it to the WEDNESDAY of that ISO week in the document year. Example: "KW 11" in year 2026 -> Wednesday of week 11, 2026 = 11.03.2026.
  * If no delivery date is mentioned at all, set DeliveryDate to null.
- "VoucherDate": the document/order confirmation date - look for "Datum", "Belegdatum", date next to "Auftragsbestatigung". Format as DD.MM.YYYY.
- For VoucherLines: extract ONLY real product/article lines. Skip shipping costs, surcharge lines, freight lines, and packaging lines UNLESS they have a real article number.
- "Number": the supplier's article/item number. Use the SUPPLIER'S number (first one listed if two exist)
- "GrossPrice": the unit LIST price exactly as printed on the PDF — do NOT apply any discount to it.
  Leave the arithmetic to the ERP. If only a net price is shown (no discount column), put it in GrossPrice and leave DiscountPercent null.
- "DiscountPercent": the discount percentage exactly as printed (e.g. 33, 35). Set to null if not shown.
  NEVER pre-calculate net price. NEVER put a calculated value in GrossPrice.
- "Quantity": number of units ordered
- "Einheit": pricing unit factor column (e.g. 1, 10, 100). If a row price is per 100 pieces, set Einheit=100.
- "Description": the FULL product description text exactly as it appears — including dimensions, sizes,
  material codes, and any suffix (e.g. "CITERDIAL 38 L 13,30" not just "CITERDIAL 38").
  Copy every word and number of the description line verbatim.
- "DescriptionUnit": the unit of measure abbreviation if shown (e.g. "M", "ST", "KG", "STK"). Null if absent.
- "VatCode": always "01"
- "Currency": CHF or EUR
- "ExternalNumber": the SUPPLIER'S own order/voucher/confirmation number — look for "Auftrags-Nr.",
  "Auftragsbestätigung Nr.", "Bestell-Nr.", "Beleg-Nr.", "unser Zeichen", "VA-Nummer", or any reference
  number the supplier assigns to this document (distinct from OurOrderNumber which starts with 2600).
  Set to null if not found.
- If a delivery date is per-line, use the earliest confirmed date as the overall DeliveryDate too
- For delivery notes (Lieferschein): treat as order confirmation, extract what's available. GrossPrice may be 0 if not shown.

You must respond ONLY with a valid JSON object - no markdown, no explanation, no extra text."""

EXTRACTION_PROMPT = """Extract the order data from this supplier PDF text and return JSON in this exact structure.
Replace ALL placeholder comments with real extracted values from the PDF text.

{{
  "Supplier": null,
  "OurOrderNumber": null,
  "ExternalNumber": null,
  "DeliveryDate": null,
  "CustomerNumber": null,
  "VoucherDate": null,
  "Currency": null,
  "AdditionalFields": [],
  "VoucherLines": [
    {{
      "$type": "E3k.Web.Objects.DataTransfer.VoucherLines.ArticleVoucherLine, E3k.Web.Objects.DataTransfer",
      "Number": null,
      "Quantity": 0.0,
      "Einheit": 1,
      "GrossPrice": 0.0,
      "DiscountPercent": null,
      "Description": null,
      "DescriptionUnit": null,
      "VatCode": "01",
      "DeliveryDate": null
    }}
  ]
}}

IMPORTANT for VoucherLines:
- "GrossPrice" = the list/gross price printed on the PDF. Do NOT calculate or modify it.
- "Einheit" = pricing unit factor from the table column "Einheit" (or equivalent). Common values: 1, 10, 100.
- "DiscountPercent" = the discount % printed on the PDF (e.g. 33 or 35). Null if not shown.
- "Description" = the COMPLETE description text, word for word, including all dimensions and codes.
- "DescriptionUnit" = unit of measure abbreviation (e.g. "M", "ST", "KG"). Null if not shown.

PDF TEXT:
---
{pdf_text}
---

Return ONLY the JSON object with real values extracted from the PDF above."""


def _sanitize(extracted: dict) -> dict:
    """
    Replace any field whose value is a known placeholder string with None.
    This guards against the LLM echoing back template text instead of real values.
    """
    def clean(value):
        if isinstance(value, str) and value.strip().lower() in _PLACEHOLDER_VALUES:
            return None
        return value

    extracted["Supplier"] = clean(extracted.get("Supplier"))
    extracted["OurOrderNumber"] = clean(extracted.get("OurOrderNumber"))
    extracted["ExternalNumber"] = clean(extracted.get("ExternalNumber"))
    extracted["VoucherDate"] = clean(extracted.get("VoucherDate"))
    extracted["DeliveryDate"] = clean(extracted.get("DeliveryDate"))
    extracted["CustomerNumber"] = clean(extracted.get("CustomerNumber"))
    extracted["Currency"] = clean(extracted.get("Currency"))

    for line in extracted.get("VoucherLines", []):
        line["Number"] = clean(line.get("Number"))
        line["Description"] = clean(line.get("Description"))
        line["DeliveryDate"] = clean(line.get("DeliveryDate"))

    return extracted


def _looks_like_supplier_name(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip(" |,;:-")
    if len(cleaned) < 3:
        return False
    lowered = cleaned.lower()
    if lowered in _RECIPIENT_BLOCKLIST:
        return False
    if "schlauchservice baumann" in lowered or "schlauch-service baumann" in lowered:
        return False
    if lowered in _GENERIC_NON_SUPPLIER_WORDS:
        return False
    if "ihren auftrag" in lowered:
        return False
    # Supplier names should not start with a lowercase stray character.
    if cleaned and cleaned[0].islower():
        return False
    if re.fullmatch(r"[0-9.,/\- ]+", cleaned):
        return False
    if re.search(_COMPANY_SUFFIX_PATTERN, cleaned, flags=re.IGNORECASE):
        return True
    # Accept shorter brand-only names if reasonably word-like.
    return bool(re.fullmatch(r"[A-Za-z0-9&.\- ]{3,60}", cleaned))


def _is_unreliable_supplier(value: str | None) -> bool:
    if not value:
        return True
    cleaned = re.sub(r"\s+", " ", str(value)).strip(" |,;:-")
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if lowered in _GENERIC_NON_SUPPLIER_WORDS:
        return True
    if "ihren auftrag" in lowered:
        return True
    if cleaned and cleaned[0].islower():
        return True
    if any(part in lowered for part in _GENERIC_NON_SUPPLIER_PARTS):
        # Business words in headers/body are frequent false positives (e.g. "Ihren Auftrag").
        # Keep only values that also carry an explicit company suffix.
        if not re.search(_COMPANY_SUFFIX_PATTERN, cleaned, flags=re.IGNORECASE):
            return True
    if len(cleaned) <= 4 and cleaned.isalpha() and cleaned[0].isupper():
        # Very short single-title words like "Betrag" are usually false positives.
        return True
    return not _looks_like_supplier_name(cleaned)


def _candidate_from_domain(domain: str) -> str | None:
    host = domain.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    root = host.split(".")[0]
    if not root or root in {"gmail", "outlook", "hotmail", "yahoo", "icloud"}:
        return None
    brand = re.sub(r"[^a-z0-9]+", " ", root).strip()
    if len(brand) < 3:
        return None
    return " ".join(part.capitalize() for part in brand.split())


def _fallback_supplier_from_text(pdf_text: str) -> str | None:
    lines = [re.sub(r"\s+", " ", line).strip() for line in pdf_text.splitlines() if line.strip()]
    if not lines:
        return None

    # 1) Explicit sender markers.
    for idx, line in enumerate(lines):
        if re.search(r"\b(absender|von|lieferant|sender)\b", line, flags=re.IGNORECASE):
            window = [line] + lines[idx + 1: idx + 4]
            for candidate in window:
                match = re.search(rf"([A-Za-z0-9&.\- ]+{_COMPANY_SUFFIX_PATTERN})", candidate, flags=re.IGNORECASE)
                if match:
                    picked = re.sub(r"\s+", " ", match.group(1)).strip(" |,;:-")
                    if not _is_unreliable_supplier(picked):
                        return picked

    # 2) Top-of-document company-style line (header first; most supplier names are here).
    for line in lines[:40]:
        match = re.search(rf"([A-Za-z0-9&.\- ]+{_COMPANY_SUFFIX_PATTERN})", line, flags=re.IGNORECASE)
        if match:
            picked = re.sub(r"\s+", " ", match.group(1)).strip(" |,;:-")
            if not _is_unreliable_supplier(picked):
                return picked

    # 3) Any remaining company-style line.
    for line in lines[40:120]:
        match = re.search(rf"([A-Za-z0-9&.\- ]+{_COMPANY_SUFFIX_PATTERN})", line, flags=re.IGNORECASE)
        if match:
            picked = re.sub(r"\s+", " ", match.group(1)).strip(" |,;:-")
            if not _is_unreliable_supplier(picked):
                return picked

    # 4) Email / URL brand fallback.
    domain_match = re.search(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", pdf_text)
    if domain_match:
        candidate = _candidate_from_domain(domain_match.group(1))
        if candidate and not _is_unreliable_supplier(candidate):
            return candidate
    web_match = re.search(r"\b(?:https?://)?(?:www\.)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", pdf_text)
    if web_match:
        candidate = _candidate_from_domain(web_match.group(1))
        if candidate and not _is_unreliable_supplier(candidate):
            return candidate
    return None


def _ocr_images_from_bytes(pdf_bytes: bytes) -> str:
    """
    OCR every raster image embedded in the PDF (logos, footer bars, etc.).

    Many supplier PDFs render the company name only inside a logo or a footer
    image that pdfplumber's text extractor cannot see.  Running Tesseract on
    each embedded image recovers that text so the LLM can identify the supplier.

    Images smaller than 100 × 30 px are skipped (decorative icons / dividers).
    Returns the concatenated OCR output, or an empty string if nothing is found.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    ocr_parts: list[str] = []
    processed_images = 0
    images_with_text = 0

    try:
        for page in doc:
            for img_info in page.get_images(full=True):
                try:
                    xref = img_info[0]
                    pix = fitz.Pixmap(doc, xref)

                    # Skip tiny decorative elements
                    if pix.width < 100 or pix.height < 30:
                        continue
                    processed_images += 1

                    # Convert via encoded PNG bytes. This is more robust than raw
                    # Image.frombytes for unusual embedded image pixel formats.
                    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    try:
                        text = pytesseract.image_to_string(img, lang="deu+eng").strip()
                    except pytesseract.TesseractError:
                        # Fallback for hosts where German language pack is not installed.
                        text = pytesseract.image_to_string(img, lang="eng").strip()
                    if text:
                        ocr_parts.append(text)
                        images_with_text += 1
                except Exception:
                    # Ignore malformed embedded images and continue processing the PDF.
                    continue
    finally:
        doc.close()

    logger.info(
        "OCR embedded images completed: processed=%d, with_text=%d",
        processed_images,
        images_with_text,
    )
    return "\n".join(ocr_parts)


def _ocr_full_pages_from_bytes(pdf_bytes: bytes) -> str:
    """
    OCR full rendered PDF pages.

    This catches cases where supplier text is visually present in the header,
    but not available as selectable text and not stored as an embedded image.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_texts: list[str] = []
    processed_pages = 0
    pages_with_text = 0
    try:
        for page in doc:
            processed_pages += 1
            # Render at 2x for better OCR accuracy.
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            try:
                text = pytesseract.image_to_string(img, lang="deu+eng").strip()
            except pytesseract.TesseractError:
                text = pytesseract.image_to_string(img, lang="eng").strip()
            if text:
                page_texts.append(text)
                pages_with_text += 1
    finally:
        doc.close()

    logger.info(
        "OCR full pages completed: processed=%d, with_text=%d",
        processed_pages,
        pages_with_text,
    )
    return "\n".join(page_texts)


def extract_text_from_bytes(pdf_bytes: bytes) -> str:
    """
    Extract all text from a PDF:
      1. Selectable text via pdfplumber (fast, accurate for text-based PDFs).
      2. Text embedded only in raster images (logos, footer bars) via Tesseract OCR.

    The OCR result is appended under a clearly labelled section header so the
    LLM prompt can instruct the model to check that section when the supplier
    name is not found in the main text.
    """
    # --- Step 1: selectable text ---
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    # --- Step 2: OCR on embedded images ---
    ocr_text = _ocr_images_from_bytes(pdf_bytes)
    if ocr_text:
        text_parts.append(f"\n=== IMAGE TEXT (OCR) ===\n{ocr_text}\n=== END IMAGE TEXT ===")

    final_text = "\n".join(text_parts)
    logger.info(
        "PDF text extraction completed: text_chars=%d, sections=%d, has_ocr=%s",
        len(final_text),
        len(text_parts),
        bool(ocr_text),
    )
    return final_text


def llm_extract(pdf_text: str) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)

    max_attempts = 6
    response = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": EXTRACTION_PROMPT.format(pdf_text=pdf_text)},
                ],
            )
            break
        except RateLimitError as exc:
            if attempt == max_attempts:
                raise
            # Parse API hint like "Please try again in 2.018s."
            wait_s = 2.0 * attempt
            msg = str(exc)
            m = re.search(r"try again in\s*([0-9]+(?:\.[0-9]+)?)s", msg, flags=re.IGNORECASE)
            if m:
                wait_s = float(m.group(1)) + 0.25
            wait_s += random.uniform(0.0, 0.35)
            logger.warning(
                "OpenAI rate limit hit (attempt %d/%d). Waiting %.2fs before retry.",
                attempt,
                max_attempts,
                wait_s,
            )
            time.sleep(wait_s)
        except (APITimeoutError, APIConnectionError) as exc:
            if attempt == max_attempts:
                raise
            wait_s = min(8.0, 1.0 * attempt) + random.uniform(0.0, 0.25)
            logger.warning(
                "Transient OpenAI error %s (attempt %d/%d). Waiting %.2fs before retry.",
                type(exc).__name__,
                attempt,
                max_attempts,
                wait_s,
            )
            time.sleep(wait_s)
        except APIStatusError as exc:
            if exc.status_code in {408, 409, 429, 500, 502, 503, 504} and attempt < max_attempts:
                wait_s = min(10.0, 1.5 * attempt) + random.uniform(0.0, 0.25)
                logger.warning(
                    "OpenAI API status %s (attempt %d/%d). Waiting %.2fs before retry.",
                    exc.status_code,
                    attempt,
                    max_attempts,
                    wait_s,
                )
                time.sleep(wait_s)
                continue
            raise

    if response is None:
        raise RuntimeError("OpenAI extraction failed after retries.")

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    def _parse_json_strict_or_salvage(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to salvage the first JSON object if extra text/noise was appended.
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = text[start:end + 1]
                return json.loads(candidate)
            raise

    try:
        extracted = _parse_json_strict_or_salvage(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "LLM returned invalid JSON (first attempt). Retrying once. error=%s snippet=%s",
            str(exc),
            raw[:500],
        )
        retry = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        EXTRACTION_PROMPT.format(pdf_text=pdf_text)
                        + "\n\nIMPORTANT: Return only strict valid JSON. No trailing commas, comments, or extra text."
                    ),
                },
            ],
        )
        raw_retry = (retry.choices[0].message.content or "").strip()
        raw_retry = re.sub(r"^```(?:json)?\s*", "", raw_retry)
        raw_retry = re.sub(r"\s*```$", "", raw_retry)
        extracted = _parse_json_strict_or_salvage(raw_retry)

    extracted = _sanitize(extracted)

    supplier_val = extracted.get("Supplier")
    if _is_unreliable_supplier(supplier_val):
        if supplier_val:
            logger.warning("Discarding unreliable supplier from LLM: supplier=%s", supplier_val)
        extracted["Supplier"] = None
        fallback_supplier = _fallback_supplier_from_text(pdf_text)
        if fallback_supplier:
            extracted["Supplier"] = fallback_supplier
            logger.info("Supplier fallback applied from PDF text: supplier=%s", fallback_supplier)
        else:
            logger.warning("Supplier fallback failed: no reliable sender name detected in PDF text.")

    return extracted


def _recover_missing_numbered_lines(pdf_text: str, extracted: dict) -> dict:
    """
    Recover line items from table-like PDF text when the LLM misses a row.
    Example row:
      2 BG1 1,00 15,00 15,00
      Bearbeitungsgebühr
    """
    existing_numbers = {
        str(line.get("Number", "")).strip().upper()
        for line in extracted.get("VoucherLines", [])
        if line.get("Number")
    }
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in pdf_text.splitlines() if ln.strip()]
    recovered = []

    row_pattern = re.compile(
        r"^\d+\s+([A-Z0-9][A-Z0-9\-_./]*)\s+(\d+,\d{2})\s+(\d+,\d{2})(?:\s+(\d+,\d{2}))?\s*$"
    )

    for idx, line in enumerate(lines):
        match = row_pattern.match(line)
        if not match:
            continue
        number = match.group(1).strip().upper()
        if number in existing_numbers:
            continue

        qty = _as_float(match.group(2), default=1.0)
        price = _as_float(match.group(3), default=0.0)
        description = ""
        if idx + 1 < len(lines):
            nxt = lines[idx + 1]
            # Description line is often plain text without table numeric tail.
            if not row_pattern.match(nxt):
                description = nxt

        recovered.append(
            {
                "$type": "E3k.Web.Objects.DataTransfer.VoucherLines.ArticleVoucherLine, E3k.Web.Objects.DataTransfer",
                "Number": number,
                "Quantity": qty,
                "GrossPrice": price,
                "DiscountPercent": None,
                "Description": description or f"Recovered line {number}",
                "DescriptionUnit": None,
                "VatCode": "01",
                "DeliveryDate": extracted.get("DeliveryDate"),
            }
        )
        existing_numbers.add(number)

    if recovered:
        extracted.setdefault("VoucherLines", []).extend(recovered)
        logger.info("Recovered %d missing numbered line(s) from PDF text table.", len(recovered))

    return extracted


def _extract_total_from_pdf_text(pdf_text: str) -> tuple[float | None, str | None]:
    # Prefer explicit final-total labels and support Swiss thousand separators (e.g. 1’106.60).
    amount_pat = r"([0-9]{1,3}(?:[’'`\s][0-9]{3})*(?:[.,][0-9]{2})|[0-9]+(?:[.,][0-9]{2}))"
    prioritized = [
        rf"(?:Gesamttotal\s*inkl\.?\s*MWST|Gesamttotal|Grand\s*Total)\s*(CHF|EUR)\s*{amount_pat}",
        rf"(?:Netto[- ]Betrag|Total|Gesamt)\s*(CHF|EUR)\s*{amount_pat}",
        rf"(CHF|EUR)\s*{amount_pat}\s*(?:Gesamttotal\s*inkl\.?\s*MWST|Gesamttotal|Netto[- ]Betrag|Total|Gesamt)",
    ]
    for pat in prioritized:
        matches = list(re.finditer(pat, pdf_text, flags=re.IGNORECASE))
        if matches:
            match = matches[-1]
            currency = match.group(1).upper()
            amount_text = match.group(2).replace("’", "").replace("'", "").replace("`", "").replace(" ", "")
            amount = _as_float(amount_text, default=0.0)
            return amount, currency
    return None, None


def _extract_total_from_last_pdf_page(pdf_bytes: bytes) -> tuple[float | None, str | None]:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return None, None
            last_page_text = pdf.pages[-1].extract_text() or ""
            return _extract_total_from_pdf_text(last_page_text)
    except Exception:
        return None, None


def _friday_of_week_from_date(date_text: str) -> str | None:
    try:
        base = datetime.strptime(date_text.strip(), "%d.%m.%Y")
    except ValueError:
        return None
    monday = base - timedelta(days=base.weekday())
    friday = monday + timedelta(days=4)
    return friday.strftime("%d.%m.%Y")


def _apply_delivery_date_fallbacks(pdf_text: str, extracted: dict) -> dict:
    voucher_date = extracted.get("VoucherDate")
    fallback = None

    # If text has relative-week phrases, default to Friday of voucher week.
    if re.search(
        r"\b(this week|diese woche|dieser woche|end of week|ende der woche|week end|wochenende)\b",
        pdf_text,
        flags=re.IGNORECASE,
    ):
        if voucher_date:
            fallback = _friday_of_week_from_date(voucher_date)

    if not fallback:
        return extracted

    if not extracted.get("DeliveryDate"):
        extracted["DeliveryDate"] = fallback

    for line in extracted.get("VoucherLines", []):
        if not line.get("DeliveryDate"):
            line["DeliveryDate"] = fallback

    return extracted


def extract_order_data(pdf_text: str, pdf_bytes: bytes) -> dict:
    """
    Run LLM extraction, then enforce supplier fallback logic.
    If supplier is still missing/unreliable, retry using full-page OCR text.
    """
    extracted = llm_extract(pdf_text)
    extracted["HasSurchargeColumn"] = bool(
        re.search(r"\b(aufschlag|surcharge)\b", pdf_text, flags=re.IGNORECASE)
    )
    supplier_val = extracted.get("Supplier")
    extracted = _recover_missing_numbered_lines(pdf_text, extracted)
    extracted = _apply_delivery_date_fallbacks(pdf_text, extracted)
    total_pdf, curr_pdf = _extract_total_from_last_pdf_page(pdf_bytes)
    if total_pdf is None:
        total_pdf, curr_pdf = _extract_total_from_pdf_text(pdf_text)
    if total_pdf is not None:
        extracted["TotalNetFromPdf"] = total_pdf
    if curr_pdf and not extracted.get("Currency"):
        extracted["Currency"] = curr_pdf
    if not _is_unreliable_supplier(supplier_val):
        return extracted

    logger.warning("Supplier unresolved after initial extraction; trying full-page OCR fallback.")
    ocr_page_text = _ocr_full_pages_from_bytes(pdf_bytes)
    if not ocr_page_text.strip():
        logger.warning("Full-page OCR produced no text.")
        return extracted

    merged_text = f"{pdf_text}\n\n=== PAGE OCR TEXT ===\n{ocr_page_text}\n=== END PAGE OCR TEXT ==="
    fallback_supplier = _fallback_supplier_from_text(merged_text)
    if fallback_supplier and not _is_unreliable_supplier(fallback_supplier):
        extracted["Supplier"] = fallback_supplier
        logger.info("Supplier recovered from full-page OCR fallback: supplier=%s", fallback_supplier)
    else:
        logger.warning("Supplier still unresolved after full-page OCR fallback.")

    extracted = _recover_missing_numbered_lines(merged_text, extracted)
    extracted = _apply_delivery_date_fallbacks(merged_text, extracted)
    return extracted


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


def build_summary(data: dict, file_name: str, folder_name: str) -> dict:
    lines = data.get("VoucherLines", [])
    summary_lines = []
    total = 0.0

    for line in lines:
        quantity = _as_float(line.get("Quantity", 0), default=0.0)
        unit_price = _as_float(
            line.get("Price", line.get("NetPrice", line.get("GrossPrice", 0))),
            default=0.0,
        )
        discount = line.get("DiscountPercent")
        discount_pct = _as_float(discount, default=0.0) if discount is not None else None
        effective_price = unit_price if discount_pct is None else unit_price * (1 - (discount_pct / 100.0))
        line_total = round(quantity * effective_price, 2)
        total += line_total

        summary_lines.append(
            {
                "number": line.get("Number"),
                "description": line.get("Description"),
                "quantity": quantity,
                "unit_price": round(unit_price, 2),
                "discount_percent": discount_pct,
                "line_total": line_total,
                "delivery_date": line.get("DeliveryDate"),
            }
        )

    total_from_pdf = data.get("TotalNetFromPdf")
    summary_total = round(_as_float(total_from_pdf, default=total), 2) if total_from_pdf is not None else round(total, 2)

    return {
        "file_name": file_name,
        "folder": folder_name,
        "supplier": data.get("Supplier", ""),
        "order_number": data.get("OurOrderNumber", ""),
        "voucher_date": data.get("VoucherDate"),
        "delivery_date": data.get("DeliveryDate"),
        "currency": data.get("Currency", "CHF"),
        "line_count": len(lines),
        "total_net": summary_total,
        "lines": summary_lines,
    }
