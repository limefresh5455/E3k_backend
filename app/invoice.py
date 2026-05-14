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

# ─────────────────────────────────────────────
# GROUP CLASSIFICATION
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# REGEX (FIXED)
# ─────────────────────────────────────────────

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
        s.replace("\u2019", "")
        .replace("'", "")
        .replace("\u2018", "")
        .replace(",", ".")
    )


# ─────────────────────────────────────────────
# PARSER
# ─────────────────────────────────────────────

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
                    pos, art, prod, qo, qd, unit, price, disc, chf_val = m.groups()

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
                        "chf": _clean(chf_val),
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


# ─────────────────────────────────────────────
# CHF FORMAT
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# PDF BUILDER
# ─────────────────────────────────────────────

def build_pdf(items, discounts, totals):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4))
    styles = getSampleStyleSheet()

    elements = []
    grouped = {}
    for i in items:
        grouped.setdefault(i["group_name"], []).append(i)

    # ── Gross per group (positive items only, before discounts) ──────────
    group_gross: dict[str, float] = {}
    for group_name, rows in grouped.items():
        group_gross[group_name] = sum(r["chf"] for r in rows)

    # Total positive gross across all groups (base for proportional split)
    total_positive_gross = sum(v for v in group_gross.values() if v > 0)

    # ── All discount lines summed (these are negative CHF values) ────────
    discounts_total = round(sum(d["chf"] for d in discounts), 2)

    # ── Resolved net / vat / grand from invoice footer ───────────────────
    if totals:
        net   = totals["net"]    # net after discounts, excl. VAT
        vat   = totals["vat"]
        grand = totals["grand"]
    else:
        gross_total = sum(group_gross.values())
        net   = gross_total + discounts_total   # discounts_total is negative
        vat   = net * 0.081
        grand = net + vat

    vat_rate = (vat / net) if net else 0.081

    # ── Distribute discounts proportionally across groups ─────────────────
    # Each group absorbs (its share of positive gross / total positive gross)
    # multiplied by the total discount amount.
    #
    # Example (from client):
    #   discounts_total = -900
    #   Hydraulik  50% → gets -450
    #   Niederdruck 40% → gets -360
    #   Luft       10% → gets -90
    #   Group net values now sum exactly to invoice net.
    #
    # To avoid floating-point drift, the last group absorbs the remainder.

    group_names_sorted = sorted(
        grouped.keys(),
        key=lambda g: {v: k for k, v in GROUPS.items()}.get(g, "999")
    )

    group_discount_share: dict[str, float] = {}
    allocated = 0.0
    for idx, group_name in enumerate(group_names_sorted):
        gross_g = group_gross.get(group_name, 0.0)
        if idx == len(group_names_sorted) - 1:
            # Last group absorbs rounding remainder
            share = round(discounts_total - allocated, 2)
        else:
            proportion = (gross_g / total_positive_gross) if total_positive_gross else 0.0
            share = round(discounts_total * proportion, 2)
        group_discount_share[group_name] = share
        allocated = round(allocated + share, 2)

    # ── Adjusted net per group (gross + proportional discount) ────────────
    group_net: dict[str, float] = {
        g: round(group_gross[g] + group_discount_share[g], 2)
        for g in grouped
    }

    # ── Distribute exact VAT and grand proportionally across groups ───────
    # Rounding each group's VAT independently causes the group totals to not
    # sum to the invoice grand total. Instead we allocate the exact invoice
    # VAT and grand proportionally (last group absorbs any cent remainder).
    group_vat:   dict[str, float] = {}
    group_total: dict[str, float] = {}

    allocated_vat   = 0.0
    allocated_total = 0.0
    for idx, group_name in enumerate(group_names_sorted):
        net_g = group_net[group_name]
        if idx == len(group_names_sorted) - 1:
            # Last group: absorb any remaining cent from rounding
            group_vat[group_name]   = round(vat   - allocated_vat,   2)
            group_total[group_name] = round(grand  - allocated_total, 2)
        else:
            proportion = (net_g / net) if net else 0.0
            gv = round(vat   * proportion, 2)
            gt = round(grand * proportion, 2)
            group_vat[group_name]   = gv
            group_total[group_name] = gt
        allocated_vat   = round(allocated_vat   + group_vat[group_name],   2)
        allocated_total = round(allocated_total + group_total[group_name], 2)

    # ── Summary table ─────────────────────────────────────────────────────
    group_code_by_name = {v: k for k, v in GROUPS.items()}
    summary_data = [["", "", "Positionen", "net price", "8.1 MWST", "total"]]

    for group_name in group_names_sorted:
        rows = grouped[group_name]
        positionen   = sum(r["qty"] for r in rows)
        net_group    = group_net[group_name]
        vat_group    = group_vat[group_name]
        total_group  = group_total[group_name]
        summary_data.append([
            group_code_by_name.get(group_name, ""),
            group_name,
            _qty_display(positionen),
            chf(net_group),
            chf(vat_group),
            chf(total_group),
        ])

    summary_data.append(["", "", "", "", "", chf(grand)])

    elements.append(Paragraph("<b>Monthly Total</b>", styles["Heading2"]))
    elements.append(Spacer(1, 6))
    summary_table = Table(summary_data, colWidths=[65, 140, 90, 90, 90, 90])
    summary_table.setStyle(TableStyle([
        ("GRID",          (0, 0),  (-1, -1), 0.5, colors.black),
        ("FONTNAME",      (0, 0),  (-1,  0), "Helvetica-Bold"),
        ("FONTNAME",      (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN",         (0, 1),  (0,  -1), "RIGHT"),
        ("ALIGN",         (2, 1),  (2,  -1), "RIGHT"),
        ("ALIGN",         (3, 1),  (5,  -1), "RIGHT"),
        ("VALIGN",        (0, 0),  (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0),  (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0),  (-1, -1), 4),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 16))

    # ── Per-group detail tables ───────────────────────────────────────────
    for group_name in group_names_sorted:
        rows = grouped[group_name]
        elements.append(Paragraph(f"<b>{group_name}</b>", styles["Heading2"]))
        elements.append(Spacer(1, 6))

        data = [["Pos", "Article", "Product", "Qty", "Unit", "Price", "CHF"]]
        raw_total = 0.0
        for r in rows:
            data.append([
                r["pos"],
                r["article_no"],
                r["product_no"],
                _qty_display(r["qty"]),
                r["unit"],
                chf(r["unit_price"]),
                chf(r["chf"]),
            ])
            raw_total += r["chf"]

        # Show discount allocation row if any discounts exist
        discount_share = group_discount_share.get(group_name, 0.0)
        if discounts_total != 0 and discount_share != 0:
            data.append([
                "", "", "Discount allocation", "", "", "", chf(discount_share)
            ])

        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("GRID",       (0, 0),  (-1, -1), 0.3, colors.grey),
            ("BACKGROUND", (0, 0),  (-1,  0), colors.lightgrey),
            ("FONTNAME",   (0, 0),  (-1,  0), "Helvetica-Bold"),
            ("ALIGN",      (3, 1),  (3,  -1), "RIGHT"),
            ("ALIGN",      (5, 1),  (6,  -1), "RIGHT"),
            ("VALIGN",     (0, 0),  (-1, -1), "MIDDLE"),
        ]))

        elements.append(table)
        # Subtotal shown is the adjusted net (after discount allocation)
        elements.append(
            Paragraph(f"Subtotal (after discount): {chf(group_net[group_name])}", styles["Normal"])
        )
        elements.append(Spacer(1, 12))

    doc.build(elements)
    buffer.seek(0)
    return buffer.read()