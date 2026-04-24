import io
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.invoice import build_pdf, parse_pdf

router = APIRouter()


@router.post("/api/invoice/parse-groups", tags=["invoice"])
async def parse_invoice_groups(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    content = await file.read()

    try:
        items, discounts, totals = parse_pdf(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Parsing failed: {e}")

    if not items:
        raise HTTPException(status_code=422, detail="No items found.")

    try:
        pdf_bytes = build_pdf(items, discounts, totals)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    filename = f"invoice_{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
