"""Dataset parsing and policy enforcement."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .constants import Audience, COMPANY_SCOPES, CompanyId
from .models import FinanceDataset, WorkerDataset


class DatasetValidationError(ValueError):
    """Raised when an uploaded dashboard dataset is not safe to publish."""


def validate_dataset(
    raw: bytes | str | dict[str, Any],
    *,
    expected_audience: Audience,
    expected_company: CompanyId,
    allow_sample: bool,
) -> dict[str, Any]:
    if expected_company not in COMPANY_SCOPES:
        raise DatasetValidationError("unsupported company")
    try:
        if isinstance(raw, bytes):
            payload = json.loads(raw.decode("utf-8"))
        elif isinstance(raw, str):
            payload = json.loads(raw)
        else:
            payload = raw
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetValidationError("dataset must be valid UTF-8 JSON") from exc

    if not isinstance(payload, dict):
        raise DatasetValidationError("dataset root must be an object")
    model_class = WorkerDataset if expected_audience == "worker" else FinanceDataset
    try:
        dataset = model_class.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"]) or "root"
        raise DatasetValidationError(
            f"dataset contract violation at {location}: {first['msg']}"
        ) from exc

    if dataset.company.id != expected_company:
        raise DatasetValidationError("dataset company does not match target path")
    if dataset.audience != expected_audience:
        raise DatasetValidationError("dataset audience does not match target path")
    if not allow_sample and dataset.isSample:
        raise DatasetValidationError("sample data cannot be published in production")
    if not dataset.isSample and dataset.source.status == "sample-only":
        raise DatasetValidationError("production data cannot use sample-only status")
    return dataset.model_dump(mode="json")
