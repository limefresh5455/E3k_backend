"""Protected configuration and dataset APIs for the separate React frontend."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
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
from .s3_dataset import S3WorkerDatasetError, S3WorkerDatasetStore
from .s3_finance_dataset import S3FinanceDatasetStore
from .storage import DatasetStore


logger = logging.getLogger("worker_dashboard.router")
router = APIRouter(dependencies=[Depends(set_private_response_headers)])
router.include_router(auth_router)
router.include_router(admin_router)


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


def _read_dataset(settings: DashboardSettings, audience: str, company: str) -> bytes:
    if _use_s3(settings):
        if audience == "worker":
            return S3WorkerDatasetStore(settings).read(company)
        if audience == "finance":
            return S3FinanceDatasetStore(settings).read(company)
    return DatasetStore(settings).read(audience, company)  # type: ignore[arg-type]


def _dataset_status(settings: DashboardSettings, audience: str, company: str) -> dict:
    if _use_s3(settings):
        if audience == "worker":
            return S3WorkerDatasetStore(settings).status(company)
        if audience == "finance":
            return S3FinanceDatasetStore(settings).status(company)
    return DatasetStore(settings).status(audience, company)  # type: ignore[arg-type]


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
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
    settings: DashboardSettings = Depends(get_settings),
):
    if audience not in {"worker", "finance"} or company not in COMPANIES:
        raise HTTPException(status_code=404, detail="Dashboard dataset not found")
    require_resource_access(principal, audience=audience, company=company)
    try:
        raw = _read_dataset(settings, audience, company)
    except (FileNotFoundError, S3WorkerDatasetError) as exc:
        logger.warning(
            "Dashboard data unavailable for %s/%s: %s",
            audience,
            company,
            exc,
        )
        raise HTTPException(status_code=503, detail="Dashboard data is not available") from exc
    return _no_store(Response(content=raw, media_type="application/json"))


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
    if audience not in {"worker", "finance"} or company not in COMPANIES:
        raise HTTPException(status_code=404, detail="Dashboard dataset not found")
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
