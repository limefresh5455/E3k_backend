import io
import json
import re

import pdfplumber
from openai import OpenAI

from app.config import OPENAI_API_KEY

SYSTEM_PROMPT = """You are a precise data extraction assistant for a Swiss hose service company (Schlauchservice Baumann GmbH).
You receive raw text extracted from supplier order confirmation PDFs (in German) and must extract structured order data.

IMPORTANT RULES:
- "OurOrderNumber" is the BUYER's order number - look for patterns like 2600xxx, BEST. 2600xxx, "Ihre Bestellung", "I/Bestellung", "Ihre Bestellnr.", "Ihr Auftrag", "Bestellreferenz"
- "CustomerNumber" is the supplier's customer number for Schlauchservice Baumann - look for "Kunden-Nr.", "Debitorennr.", "Kundennr.", "Kunden NR.", "Ihre Kunden-Nr.", "Kundennummer"
- All dates in the output must use the format DD.MM.YYYY (e.g. 18.03.2026). Convert ALL date formats to this.
- "DeliveryDate": the confirmed delivery/dispatch date - look for "Lieferung/Termin", "Auslieferdatum", "Versandtermin", "Lieferung", "Termin best.", "Warenausgangsdatum", "Versand-Datum", "Liefertermin"
  * If the delivery date is given as a calendar week like "KW 11" or "KW11", convert it to the WEDNESDAY of that ISO week in the document year. Example: "KW 11" in year 2026 -> Wednesday of week 11, 2026 = 11.03.2026.
  * If no delivery date is mentioned at all, set DeliveryDate to null.
- "VoucherDate": the document/order confirmation date - look for "Datum", "Belegdatum", date next to "Auftragsbestatigung". Format as DD.MM.YYYY.
- For VoucherLines: extract ONLY real product/article lines. Skip shipping costs, surcharge lines, freight lines, and packaging lines UNLESS they have a real article number.
- "Number": the supplier's article/item number. Use the SUPPLIER'S number (first one listed if two exist)
- "Price": unit purchase price. Use net/discounted price if available. If only gross + discount %, calculate: price x (1 - discount/100)
- "Quantity": number of units ordered
- "Description": product description text
- "VatCode": always "01"
- "Currency": CHF or EUR
- If a delivery date is per-line, use the earliest confirmed date as the overall DeliveryDate too
- For delivery notes (Lieferschein): treat as order confirmation, extract what's available. Price may be 0 if not shown.

You must respond ONLY with a valid JSON object - no markdown, no explanation, no extra text."""

EXTRACTION_PROMPT = """Extract the order data from this supplier PDF text and return JSON matching this exact structure:

{{
  "Supplier": "supplier company name",
  "OurOrderNumber": "2600xxx",
  "DeliveryDate": "DD.MM.YYYY or null",
  "CustomerNumber": "customer number or null",
  "VoucherDate": "DD.MM.YYYY or null",
  "Currency": "CHF or EUR",
  "AdditionalFields": [],
  "VoucherLines": [
    {{
      "$type": "E3k.Web.Objects.DataTransfer.VoucherLines.ArticleVoucherLine, E3k.Web.Objects.DataTransfer",
      "Number": "article number",
      "Quantity": 1.0,
      "Price": 0.0,
      "Description": "product description",
      "VatCode": "01",
      "DeliveryDate": "DD.MM.YYYY or null"
    }}
  ]
}}

PDF TEXT:
---
{pdf_text}
---

Return ONLY the JSON object."""


def extract_text_from_bytes(pdf_bytes: bytes) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                parts.append(page_text)
    return "\n".join(parts)


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
    return json.loads(raw)


def build_summary(data: dict, file_name: str, folder_name: str) -> dict:
    lines = data.get("VoucherLines", [])
    total = sum(float(line.get("Price", 0)) * float(line.get("Quantity", 0)) for line in lines)
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
    }

