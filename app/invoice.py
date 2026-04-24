import re
import io
import pdfplumber

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse

from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet

from datetime import datetime, timezone

router = APIRouter()

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# GROUP CLASSIFICATION
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

HYDRAULIC_PREFIXES = {"48", "49", "38", "75", "39", "51", "58", "47", "77"}
AIR_PREFIXES = {"74"}

GROUPS = {
    "100": "Hydraulik",
    "200": "Niederdruck",
    "300": "Luft",
}


def _classify(article_no: str) -> str:
    digits = article_no.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    prefix = digits[:2]

    if prefix in HYDRAULIC_PREFIXES:
        return "100"
    if prefix in AIR_PREFIXES:
        return "300"
    return "200"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# REGEX (FIXED)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

LINE_RE = re.compile(
    r"^(\d{1,3})\s*"
    r"([A-Z]?\d{6,7})\s+"
    r"(.+?)\s+"
    r"([\d\u2019'.]+)/([\d\u2019'.]+)\s+"
    r"(Stk|m)\s+"
    r"([\d\u2019'.]+)"
    r"(?:\s*/[\d]+\s*\w+)?"
    r"\s+"
    r"(?:([\d]+%|Kostenlos|Ersatzlieferung)\s+)?"
    r"(-?[\d\u2019'.,]+)$"
)

LINE_DISCOUNT = re.compile(
    r"^(90[0-9]{3}-\d+)\s+\d\s+(-?[\d\u2019'.,]+)$"
)

LINE_SURCHARGE = re.compile(
    r"^(HZ\w+)\s+\d+\s+Stk\s+([\d\u2019'.]+)\s+([\d\u2019'.]+)"
)

TOTAL_RE = re.compile(
    r"Positions-Nettototal ohne MWST\s+CHF\s+([\d\u2019'.,]+).*?"
    r"\d+(?:[.,]\d+)?% MWST\s+CHF\s+[\d\u2019'.,]+\s+CHF\s+([\d\u2019'.,]+).*?"
    r"Gesamt-Total inkl\. MWST\s+CHF\s+([\d\u2019'.,]+)",
    re.S,
)


def _clean(s: str) -> float:
    return float(
        s.replace("â€™", "")
        .replace("’", "")
        .replace("'", "")
        .replace(",", ".")
    )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PARSER
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def parse_pdf(file_bytes: bytes):
    items = []
    discounts = []
    full_text = ""

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            full_text += text + "\n"

            for line in text.splitlines():
                line = line.strip()

                m = LINE_RE.match(line)
                if m:
                    pos, art, prod, qo, qd, unit, price, disc, chf = m.groups()

                    if art.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").startswith("90"):
                        continue

                    group_code = _classify(art)

                    items.append({
                        "pos": int(pos),
                        "article_no": art,
                        "product_no": prod.strip(),
                        "qty": _clean(qd),
                        "unit": unit,
                        "unit_price": _clean(price),
                        "discount": disc or "",
                        "chf": _clean(chf),
                        "group_name": GROUPS[group_code],
                    })
                    continue

                md = LINE_DISCOUNT.match(line)
                if md:
                    discounts.append({
                        "chf": _clean(md.group(2)),
                    })
                    continue

                ms = LINE_SURCHARGE.match(line)
                if ms:
                    code, price, total = ms.groups()

                    items.append({
                        "pos": 0,
                        "article_no": code,
                        "product_no": "Surcharge",
                        "qty": 1,
                        "unit": "Stk",
                        "unit_price": _clean(price),
                        "discount": "",
                        "chf": _clean(total),
                        "group_name": GROUPS["200"],
                    })
                    continue

    totals = None
    m = TOTAL_RE.search(full_text)
    if m:
        net, vat, grand = m.groups()
        totals = {
            "net": _clean(net),
            "vat": _clean(vat),
            "grand": _clean(grand),
        }

    return items, discounts, totals


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CHF FORMAT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def chf(v):
    neg = v < 0
    v = abs(round(v, 2))
    i = int(v)
    d = int(round((v - i) * 100))
    s = f"{i:,}".replace(",", "'") + f".{d:02d}"
    return f"-{s}" if neg else s


def _qty_display(v):
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PDF BUILDER (FIXED TOTAL LOGIC)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def build_pdf(items, discounts, totals):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4))
    styles = getSampleStyleSheet()

    elements = []
    grouped = {}
    for i in items:
        grouped.setdefault(i["group_name"], []).append(i)

    net_items = sum(i["chf"] for i in items)
    discounts_total = round(sum(d["chf"] for d in discounts), 2)

    if totals:
        # Use the totals printed in the invoice summary as source of truth.
        net = totals["net"]
        vat = totals["vat"]
        grand = totals["grand"]
        gross = net_items

    else:
        gross = net_items
        net = gross + discounts_total
        vat = net * 0.081
        grand = net + vat

    vat_rate = (vat / net) if net else 0.081
    summary_data = [["", "", "Positionen", "net price", "8.1 MWST", "total"]]

    group_code_by_name = {v: k for k, v in GROUPS.items()}
    for group_name in sorted(grouped.keys(), key=lambda g: group_code_by_name.get(g, "999")):
        rows = grouped[group_name]
        positionen = sum(r["qty"] for r in rows)
        net_group = round(sum(r["chf"] for r in rows), 2)
        vat_group = round(net_group * vat_rate, 2)
        total_group = round(net_group + vat_group, 2)
        summary_data.append([
            group_code_by_name.get(group_name, ""),
            group_name,
            _qty_display(positionen),
            chf(net_group),
            chf(vat_group),
            chf(total_group),
        ])

    summary_data.append(["", "", "", "", "", chf(grand)])

    elements.append(Paragraph("<b>Montly Total</b>", styles["Heading2"]))
    elements.append(Spacer(1, 6))
    summary_table = Table(summary_data, colWidths=[65, 140, 90, 90, 90, 90])
    summary_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, 1), (0, -1), "RIGHT"),
        ("ALIGN", (2, 1), (2, -1), "RIGHT"),
        ("ALIGN", (3, 1), (5, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 16))

    # group tables (after summary so summary stays on page 1)
    for group, rows in grouped.items():
        elements.append(Paragraph(f"<b>{group}</b>", styles["Heading2"]))
        elements.append(Spacer(1, 6))

        data = [["Pos", "Article", "Product", "Qty", "Unit", "Price", "CHF"]]
        total = 0
        for r in rows:
            data.append([
                r["pos"],
                r["article_no"],
                r["product_no"],
                r["qty"],
                r["unit"],
                chf(r["unit_price"]),
                chf(r["chf"]),
            ])
            total += r["chf"]

        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (3, 1), (3, -1), "RIGHT"),
            ("ALIGN", (5, 1), (6, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))

        elements.append(table)
        elements.append(Paragraph(f"Subtotal: {chf(total)}", styles["Normal"]))
        elements.append(Spacer(1, 12))

    doc.build(elements)
    buffer.seek(0)
    return buffer.read()

