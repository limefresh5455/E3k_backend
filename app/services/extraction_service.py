import io
import json
import re

import fitz  # PyMuPDF
import pdfplumber
import pytesseract
from openai import OpenAI
from PIL import Image

from app.config import OPENAI_API_KEY

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

    try:
        for page in doc:
            for img_info in page.get_images(full=True):
                try:
                    xref = img_info[0]
                    pix = fitz.Pixmap(doc, xref)

                    # Skip tiny decorative elements
                    if pix.width < 100 or pix.height < 30:
                        continue

                    # Convert via encoded PNG bytes. This is more robust than raw
                    # Image.frombytes for unusual embedded image pixel formats.
                    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    text = pytesseract.image_to_string(img, lang="deu+eng").strip()
                    if text:
                        ocr_parts.append(text)
                except Exception:
                    # Ignore malformed embedded images and continue processing the PDF.
                    continue
    finally:
        doc.close()

    return "\n".join(ocr_parts)


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

    return "\n".join(text_parts)


def llm_extract(pdf_text: str) -> dict:
    client = OpenAI(api_key=OPENAI_API_KEY)
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

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    extracted = json.loads(raw)
    return _sanitize(extracted)


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
        unit_price = _as_float(line.get("GrossPrice", line.get("Price", 0)), default=0.0)
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

    return {
        "file_name": file_name,
        "folder": folder_name,
        "supplier": data.get("Supplier", ""),
        "order_number": data.get("OurOrderNumber", ""),
        "voucher_date": data.get("VoucherDate"),
        "delivery_date": data.get("DeliveryDate"),
        "currency": data.get("Currency", "CHF"),
        "line_count": len(lines),
        "total_net": round(total, 2),
        "lines": summary_lines,
    }
