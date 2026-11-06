
"""S3 data link endpoint for the Europa3000 live dashboard.

Reads current.json from the S3 bucket, looks up the file for the requested
role/company and returns a short-lived presigned download link.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.config import Config
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .access import (
    DashboardPrincipal,
    require_dashboard_principal,
    require_resource_access,
    set_private_response_headers,
)
from .constants import COMPANIES

logger = logging.getLogger("worker_dashboard.s3")

router = APIRouter(
    dependencies=[Depends(set_private_response_headers)]
)

BUCKET = os.getenv("S3_BUCKET", "e3k-backend")
REGION = os.getenv("AWS_REGION", "eu-north-1")
MANIFEST_KEY = os.getenv("S3_MANIFEST_KEY", "current.json")
LINK_TTL_SECONDS = 300

_s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=Config(
        signature_version="s3v4",
        retries={"max_attempts": 3},
    ),
)

# Dashboard URL uses "workers"; access layer uses "worker".
_AUDIENCE = {
    "workers": "worker",
    "finance": "finance",
}


@router.get("/api/s3-data/{role}/{company}")
def data_link(
    role: str,
    company: str,
    principal: DashboardPrincipal = Depends(
        require_dashboard_principal
    ),
):
    logger.info(
        "S3 API request received: role=%r, company=%r",
        role,
        company,
    )

    # Log allowed values to debug 404 validation errors.
    logger.info(
        "Allowed roles: %s",
        list(_AUDIENCE.keys()),
    )
    logger.info(
        "Allowed companies: %s",
        list(COMPANIES.keys())
        if isinstance(COMPANIES, dict)
        else COMPANIES,
    )

    # Step 1: Validate role and company.
    if role not in _AUDIENCE:
        logger.warning(
            "S3 API rejected request: invalid role=%r",
            role,
        )
        raise HTTPException(
            status_code=404,
            detail="Dashboard dataset not found",
        )

    if company not in COMPANIES:
        logger.warning(
            "S3 API rejected request: invalid company=%r",
            company,
        )
        raise HTTPException(
            status_code=404,
            detail="Dashboard dataset not found",
        )

    logger.info(
        "Role/company validation passed: %s/%s",
        role,
        company,
    )

    # Step 2: Check user permission.
    audience = _AUDIENCE[role]

    try:
        require_resource_access(
            principal,
            audience=audience,
            company=company,
        )

        logger.info(
            "Resource access check passed: audience=%s, company=%s",
            audience,
            company,
        )

    except HTTPException as exc:
        logger.warning(
            "Resource access denied: role=%s, audience=%s, "
            "company=%s, status=%s, detail=%s",
            role,
            audience,
            company,
            exc.status_code,
            exc.detail,
        )
        raise

    # Step 3: Read latest S3 manifest.
    try:
        logger.info(
            "Reading S3 manifest: bucket=%s, key=%s",
            BUCKET,
            MANIFEST_KEY,
        )

        response = _s3.get_object(
            Bucket=BUCKET,
            Key=MANIFEST_KEY,
        )

        raw = response["Body"].read()
        manifest = json.loads(raw)

        manifest_entry_key = f"{role}/{company}"

        logger.info(
            "Looking up manifest entry: %s",
            manifest_entry_key,
        )

        entry = manifest["objects"][manifest_entry_key]

        key = entry["key"]
        sha256 = entry["sha256"]
        fetched_at = entry["sourceFetchedAt"]
        published_at = manifest.get("publishedAt")

        logger.info(
            "S3 manifest entry found: %s, object_key=%s",
            manifest_entry_key,
            key,
        )

    except Exception:
        logger.exception(
            "Could not read S3 manifest or find dataset for %s/%s",
            role,
            company,
        )
        raise HTTPException(
            status_code=503,
            detail="Data not available",
        )

    # Step 4: Validate S3 object key.
    if (
        not isinstance(key, str)
        or not key.startswith("generations/")
        or ".." in key
    ):
        logger.error(
            "Rejected unexpected S3 object key: %r",
            key,
        )
        raise HTTPException(
            status_code=503,
            detail="Data not available",
        )

    # Step 5: Generate presigned URL.
    try:
        url = _s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": BUCKET,
                "Key": key,
            },
            ExpiresIn=LINK_TTL_SECONDS,
        )

        logger.info(
            "Presigned URL generated successfully for %s/%s",
            role,
            company,
        )

    except Exception:
        logger.exception(
            "Failed to generate presigned URL for %s/%s",
            role,
            company,
        )
        raise HTTPException(
            status_code=503,
            detail="Data not available",
        )

    return JSONResponse(
        {
            "url": url,
            "sha256": sha256,
            "sourceFetchedAt": fetched_at,
            "publishedAt": published_at,
        },
        headers={
            "Cache-Control": "no-store",
        },
    )
