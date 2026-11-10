"""Protected configuration and dataset APIs for the separate React frontend."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .access import (
    DashboardPrincipal,
    get_settings,
    require_dashboard_principal,
    require_resource_access,
    set_private_response_headers,
)
from .admin_api import router as admin_router
from .auth_api import router as auth_router
from .config import DashboardSettings
from .constants import COMPANIES
from .models import FinanceDataset, WorkerDataset
from .schemas import DashboardConfigResponse, DashboardStatusResponse
from .s3_cache import CachedDataset
from .s3_dataset import S3WorkerDatasetError, S3WorkerDatasetStore
from .s3_finance_dataset import S3FinanceDatasetStore
from .storage import DatasetStore


logger = logging.getLogger("worker_dashboard.router")
router = APIRouter(dependencies=[Depends(set_private_response_headers)])
router.include_router(auth_router)
router.include_router(admin_router)

WORKER_SECTIONS = {
    "customers": "customers",
    "articles": "articles",
    "sales-quantities": "salesQuantities",
    "salesQuantities": "salesQuantities",
    "documents": "documents",
    "warehouse-movements": "warehouseMovements",
    "warehouseMovements": "warehouseMovements",
}
FINANCE_SECTIONS = {
    "cash": "cash",
    "receivables": "receivables",
    "payables": "payables",
    "monthly-results": "monthlyResults",
    "monthlyResults": "monthlyResults",
    "inventory-valuation": "inventoryValuation",
    "inventoryValuation": "inventoryValuation",
}


def _no_store(response: Response) -> Response:
    set_private_response_headers(response)
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def _use_s3(settings: DashboardSettings) -> bool:
    # Both worker and finance datasets come from S3 in production.
    return settings.environment == "production"


def _s3_store(settings: DashboardSettings, audience: str):
    if audience == "finance":
        return S3FinanceDatasetStore(settings)
    return S3WorkerDatasetStore(settings)


def _dataset_status(settings: DashboardSettings, audience: str, company: str) -> dict:
    if _use_s3(settings):
        return _s3_store(settings, audience).status(company)
    return DatasetStore(settings).status(audience, company)  # type: ignore[arg-type]


def _read_dataset_payload(
    settings: DashboardSettings,
    audience: str,
    company: str,
) -> dict[str, Any]:
    if _use_s3(settings):
        raw = _s3_store(settings, audience).read(company)
    else:
        raw = DatasetStore(settings).read(audience, company)  # type: ignore[arg-type]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise S3WorkerDatasetError("Dashboard dataset cache contains invalid JSON") from error
    if not isinstance(payload, dict):
        raise S3WorkerDatasetError("Dashboard dataset root is invalid")
    return payload


def _json_response(payload: dict[str, Any]) -> Response:
    return _no_store(
        Response(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            media_type="application/json",
        )
    )


def _accepts_gzip(header: str | None) -> bool:
    for part in (header or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() not in {"gzip", "x-gzip", "*"}:
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return True
    return False


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    wanted = etag.removeprefix("W/")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == wanted:
            return True
    return False


def _cached_dataset_response(request: Request, cached: CachedDataset) -> Response:
    """Serve the pre-compressed dataset; 304 when the client already has it."""

    headers = {"ETag": cached.etag, "Vary": "Accept-Encoding"}
    if _etag_matches(request.headers.get("if-none-match"), cached.etag):
        return _no_store(Response(status_code=304, headers=headers))
    if _accepts_gzip(request.headers.get("accept-encoding")):
        headers["Content-Encoding"] = "gzip"
        content = cached.gzip_body
    else:
        content = cached.json_bytes()
    return _no_store(
        Response(content=content, media_type="application/json", headers=headers)
    )


def _validate_dataset_route(audience: str, company: str) -> None:
    if audience not in {"worker", "finance"} or company not in COMPANIES:
        raise HTTPException(status_code=404, detail="Dashboard dataset not found")


def _source_summary(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload.get("source")
    if not isinstance(source, dict):
        source = {}
    return {
        "status": source.get("status"),
        "asOf": source.get("asOf"),
        "exportedAt": source.get("exportedAt"),
        "historyFrom": source.get("historyFrom"),
    }


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _worker_summary(payload: dict[str, Any], company: str) -> dict[str, Any]:
    articles = payload.get("articles")
    if not isinstance(articles, list):
        articles = []
    low_stock = sum(
        1
        for article in articles
        if isinstance(article, dict)
        and _number(article.get("currentStock")) < _number(article.get("minStock"))
    )
    return {
        "audience": "worker",
        "company": company,
        "source": _source_summary(payload),
        "counts": {
            "customers": _count(payload, "customers"),
            "articles": len(articles),
            "salesQuantities": _count(payload, "salesQuantities"),
            "documents": _count(payload, "documents"),
            "warehouseMovements": _count(payload, "warehouseMovements"),
        },
        "stock": {
            "lowStockCount": low_stock,
        },
    }


def _finance_summary(payload: dict[str, Any], company: str) -> dict[str, Any]:
    receivables = _list(payload.get("receivables"))
    payables = _list(payload.get("payables"))
    cash = payload.get("cash")
    cash_accounts = _list(cash.get("accounts") if isinstance(cash, dict) else None)
    inventory = payload.get("inventoryValuation")
    if not isinstance(inventory, dict):
        inventory = {}
    return {
        "audience": "finance",
        "company": company,
        "source": _source_summary(payload),
        "currency": payload.get("currency", "CHF"),
        "cash": {
            "accountCount": len(cash_accounts),
            "liquidityCents": sum(
                _int(account.get("balanceCents"))
                for account in cash_accounts
                if isinstance(account, dict) and account.get("includeInLiquidity") is True
            ),
            "confirmed": cash.get("confirmed") if isinstance(cash, dict) else None,
        },
        "receivables": _open_item_summary(receivables),
        "payables": _open_item_summary(payables),
        "inventoryValuation": {
            "status": inventory.get("status"),
            "valueCents": inventory.get("valueCents"),
            "reason": inventory.get("reason"),
        },
        "monthlyResults": {
            "count": _count(payload, "monthlyResults"),
        },
    }


def _open_item_summary(rows: list[Any]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "outstandingCents": sum(
            _int(row.get("outstandingAmountCents"))
            for row in rows
            if isinstance(row, dict)
        ),
        "openCount": _status_count(rows, "open"),
        "overdueCount": _status_count(rows, "overdue"),
        "paidCount": _status_count(rows, "paid"),
        "partiallyPaidCount": _status_count(rows, "partially_paid"),
    }


def _status_count(rows: list[Any], status: str) -> int:
    return sum(1 for row in rows if isinstance(row, dict) and row.get("status") == status)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number(value: Any) -> int | float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _section_key(audience: str, section: str) -> str:
    sections = FINANCE_SECTIONS if audience == "finance" else WORKER_SECTIONS
    key = sections.get(section)
    if key is None:
        raise HTTPException(status_code=404, detail="Dashboard section not found")
    return key


def _page_response(
    payload: dict[str, Any],
    *,
    audience: str,
    company: str,
    section: str,
    limit: int,
    offset: int,
    search: str | None,
    status: str | None,
    sort: str | None,
    direction: str,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    key = _section_key(audience, section)
    value = payload.get(key)
    if not isinstance(value, list):
        return {
            "audience": audience,
            "company": company,
            "section": section,
            "source": _source_summary(payload),
            "item": value,
        }
    rows = _filter_rows(
        value,
        search=search,
        status=status,
        from_date=from_date,
        to_date=to_date,
    )
    if sort:
        rows = sorted(
            rows,
            key=lambda item: _sort_value(item, sort),
            reverse=direction.lower() == "desc",
        )
    total = len(rows)
    items = rows[offset : offset + limit]
    return {
        "audience": audience,
        "company": company,
        "section": section,
        "source": _source_summary(payload),
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + limit < total,
    }


def _filter_rows(
    rows: list[Any],
    *,
    search: str | None,
    status: str | None,
    from_date: str | None,
    to_date: str | None,
) -> list[Any]:
    filtered = rows
    if status:
        wanted = status.strip().lower()
        filtered = [
            row
            for row in filtered
            if isinstance(row, dict) and str(row.get("status", "")).lower() == wanted
        ]
    if from_date or to_date:
        filtered = [
            row
            for row in filtered
            if isinstance(row, dict)
            and _date_in_range(_row_date(row), from_date=from_date, to_date=to_date)
        ]
    if search:
        needle = search.strip().lower()
        if needle:
            filtered = [
                row
                for row in filtered
                if isinstance(row, dict) and _row_matches_search(row, needle)
            ]
    return filtered


def _row_date(row: dict[str, Any]) -> str | None:
    for key in ("date", "issueDate", "dueDate", "period"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return None


def _date_in_range(
    value: str | None,
    *,
    from_date: str | None,
    to_date: str | None,
) -> bool:
    if value is None:
        return False
    if from_date and value < from_date:
        return False
    if to_date and value > to_date:
        return False
    return True


def _row_matches_search(row: dict[str, Any], needle: str) -> bool:
    for value in row.values():
        if isinstance(value, (str, int, float)) and needle in str(value).lower():
            return True
    return False


def _sort_value(item: Any, key: str) -> tuple[int, str | float]:
    if not isinstance(item, dict):
        return (3, "")
    value = item.get(key)
    if value is None:
        return (3, "")
    if isinstance(value, bool):
        return (2, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, str):
        return (1, value)
    return (2, str(value))


@router.get(
    "/api/worker-dashboard/config",
    response_model=DashboardConfigResponse,
)
def dashboard_config(
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
):
    companies = [
        {
            "id": company,
            "label": COMPANIES[company]["name"],
            "workerDataUrl": f"/api/worker-dashboard/data/worker/{company}",
            "financeDataUrl": (
                f"/api/worker-dashboard/data/finance/{company}"
                if principal.can_read("finance", company)
                else None
            ),
        }
        for company in principal.companies
    ]
    return {
        "environment": settings.environment,
        "pollIntervalMs": 300_000,
        "maxStaleMinutes": 15,
        "companies": companies,
    }


@router.get(
    "/api/worker-dashboard/data/{audience}/{company}",
    response_model=WorkerDataset | FinanceDataset,
)
def dashboard_data(
    audience: str,
    company: str,
    request: Request,
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
):
    _validate_dataset_route(audience, company)
    require_resource_access(principal, audience=audience, company=company)
    try:
        if _use_s3(settings):
            cached = _s3_store(settings, audience).read_cached(company)
            return _cached_dataset_response(request, cached)
        raw = DatasetStore(settings).read(audience, company)  # type: ignore[arg-type]
    except (FileNotFoundError, S3WorkerDatasetError) as exc:
        logger.warning(
            "Dashboard data unavailable for %s/%s: %s",
            audience,
            company,
            exc,
        )
        raise HTTPException(status_code=503, detail="Dashboard data is not available") from exc
    return _no_store(Response(content=raw, media_type="application/json"))


@router.get("/api/worker-dashboard/summary/{audience}/{company}")
def dashboard_summary(
    audience: str,
    company: str,
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
):
    _validate_dataset_route(audience, company)
    require_resource_access(principal, audience=audience, company=company)
    try:
        payload = _read_dataset_payload(settings, audience, company)
    except (FileNotFoundError, S3WorkerDatasetError) as exc:
        logger.warning(
            "Dashboard summary unavailable for %s/%s: %s",
            audience,
            company,
            exc,
        )
        raise HTTPException(status_code=503, detail="Dashboard data is not available") from exc
    if audience == "finance":
        return _json_response(_finance_summary(payload, company))
    return _json_response(_worker_summary(payload, company))


@router.get("/api/worker-dashboard/data/{audience}/{company}/{section}")
def dashboard_section_data(
    audience: str,
    company: str,
    section: str,
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200),
    status: str | None = Query(None, max_length=100),
    sort: str | None = Query(None, max_length=100),
    direction: str = Query("asc", pattern="^(asc|desc)$"),
    from_date: str | None = Query(None, alias="from", max_length=10),
    to_date: str | None = Query(None, alias="to", max_length=10),
):
    _validate_dataset_route(audience, company)
    require_resource_access(principal, audience=audience, company=company)
    try:
        payload = _read_dataset_payload(settings, audience, company)
        response = _page_response(
            payload,
            audience=audience,
            company=company,
            section=section,
            limit=limit,
            offset=offset,
            search=search,
            status=status,
            sort=sort,
            direction=direction,
            from_date=from_date,
            to_date=to_date,
        )
    except HTTPException:
        raise
    except (FileNotFoundError, S3WorkerDatasetError) as exc:
        logger.warning(
            "Dashboard section unavailable for %s/%s/%s: %s",
            audience,
            company,
            section,
            exc,
        )
        raise HTTPException(status_code=503, detail="Dashboard data is not available") from exc
    return _json_response(response)


@router.get(
    "/api/worker-dashboard/status/{audience}/{company}",
    response_model=DashboardStatusResponse,
)
def dashboard_status(
    audience: str,
    company: str,
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
):
    _validate_dataset_route(audience, company)
    require_resource_access(principal, audience=audience, company=company)
    try:
        status_payload = _dataset_status(settings, audience, company)
    except S3WorkerDatasetError as exc:
        logger.warning(
            "Dashboard status unavailable for %s/%s: %s",
            audience,
            company,
            exc,
        )
        raise HTTPException(status_code=503, detail="Dashboard data is not available") from exc
    safe_fields = {
        key: status_payload.get(key)
        for key in (
            "audience",
            "company",
            "status",
            "source_as_of",
            "source_exported_at",
            "published_at",
        )
        if key in status_payload
    }
    return _no_store(
        Response(
            content=json.dumps(safe_fields),
            media_type="application/json",
        )
    )
