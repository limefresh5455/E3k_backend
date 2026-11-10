"""Read, verify and validate finance datasets from S3.

Finance counterpart of ``s3_dataset.S3WorkerDatasetStore``.  The worker loader
cannot be reused for finance because it expands the compact
``europa3000-workers-v2`` export, whereas the finance object is checked
against the ``FinanceDataset`` dashboard contract.

Manifest entry used: ``objects["finance/<company>"]`` (same entry that
``s3_api.py`` already serves through ``/api/s3-data/finance/<company>``).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
from io import BytesIO
import json
import os
from threading import Lock
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import DashboardSettings
from .constants import COMPANIES
from .s3_dataset import (
    MANIFEST_MAX_BYTES,
    S3WorkerDatasetError,
    _optional_text,
    _read_stream_limited,
    _required_sha256,
    _required_text,
    _utc_timestamp,
)
from .validation import DatasetValidationError, validate_dataset


# Like the worker loader, S3 is the source of truth for finance data, and the
# finance objects currently in the bucket may still be flagged as sample data
# (isSample=true / status "sample-only").  Allowed by default so they are served;
# set WORKER_DASHBOARD_S3_FINANCE_ALLOW_SAMPLE=false once real data is published.
_ALLOW_SAMPLE = os.getenv(
    "WORKER_DASHBOARD_S3_FINANCE_ALLOW_SAMPLE", "true"
).strip().lower() in {"1", "true", "yes", "on"}

_CACHE_LOCK = Lock()
_CACHE: dict[tuple[str, str, str], bytes] = {}


class S3FinanceDatasetError(S3WorkerDatasetError):
    """Raised when the selected finance S3 generation is unavailable or invalid.

    Subclasses S3WorkerDatasetError so existing ``except`` clauses in the
    router handle it without changes.
    """


class S3FinanceDatasetStore:
    def __init__(self, settings: DashboardSettings, s3_client=None):
        self.settings = settings
        self.s3 = s3_client or boto3.client(
            "s3",
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def read(self, company: str) -> bytes:
        if company not in COMPANIES:
            raise S3FinanceDatasetError("Unsupported dashboard company")

        manifest, entry = self._manifest_entry(company)
        object_key = _required_text(entry, "key")
        expected_digest = _required_sha256(entry.get("sha256"))
        _validate_object_key(object_key, company)

        cache_key = (self.settings.s3_bucket, object_key, expected_digest)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

        response = self._get_object(object_key, "finance object")
        raw_object = _read_stream_limited(
            response.get("Body"),
            self.settings.max_dataset_bytes,
            "S3 finance object",
        )
        # Same rule as the worker loader / s3_api: the manifest digest is over
        # the stored bytes (compressed if the object is .gz).
        actual_digest = hashlib.sha256(raw_object).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise S3FinanceDatasetError("S3 finance object checksum does not match manifest")

        decoded = _decode_limited(raw_object, self.settings.max_dataset_bytes)
        payload = _parse_json_object(decoded)

        try:
            validated = validate_dataset(
                payload,
                expected_audience="finance",
                expected_company=company,  # type: ignore[arg-type]
                allow_sample=_ALLOW_SAMPLE,
            )
        except DatasetValidationError as error:
            raise S3FinanceDatasetError(
                f"S3 finance dataset does not satisfy dashboard contract: {error} "
                f"(top-level keys: {sorted(payload)[:12]}, "
                f"format={payload.get('format')!r}, "
                f"schemaVersion={payload.get('schemaVersion')!r})"
            ) from error

        canonical = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        with _CACHE_LOCK:
            # Two companies x rolling generation; keep the cache bounded.
            if len(_CACHE) >= 4:
                _CACHE.clear()
            _CACHE[cache_key] = canonical
        return canonical

    def status(self, company: str) -> dict[str, Any]:
        manifest, entry = self._manifest_entry(company)
        payload = json.loads(self.read(company))
        source = payload["source"]
        status: dict[str, Any] = {
            "audience": "finance",
            "company": company,
            "status": source["status"],
            "source_as_of": source["asOf"],
            "source_exported_at": source["exportedAt"],
        }
        published_at = _optional_text(manifest.get("publishedAt"))
        if published_at:
            status["published_at"] = _utc_timestamp(published_at)
        return status

    def _manifest_entry(self, company: str) -> tuple[dict[str, Any], dict[str, Any]]:
        response = self._get_object(self.settings.s3_manifest_key, "manifest")
        raw = _read_stream_limited(
            response.get("Body"),
            MANIFEST_MAX_BYTES,
            "S3 manifest",
        )
        try:
            manifest = json.loads(raw.decode("utf-8"))
            entry = manifest["objects"][f"finance/{company}"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise S3FinanceDatasetError(
                f"S3 manifest has no valid finance/{company} entry"
            ) from error
        if not isinstance(manifest, dict) or not isinstance(entry, dict):
            raise S3FinanceDatasetError("S3 manifest finance entry is invalid")
        return manifest, entry

    def _get_object(self, key: str, label: str) -> dict[str, Any]:
        try:
            return self.s3.get_object(Bucket=self.settings.s3_bucket, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise S3FinanceDatasetError(f"Could not read S3 dashboard {label}") from error


def _validate_object_key(key: str, company: str) -> None:
    allowed_suffixes = (
        f"/finance/{company}.json.gz",
        f"/finance/{company}.json",
    )
    if (
        not key.startswith("generations/")
        or ".." in key
        or not key.endswith(allowed_suffixes)
    ):
        raise S3FinanceDatasetError("S3 manifest contains an unexpected finance key")


def _decode_limited(raw: bytes, maximum: int) -> bytes:
    """Accept gzip (like the worker objects) or plain JSON."""

    if not raw.startswith(b"\x1f\x8b"):
        if len(raw) > maximum:
            raise S3FinanceDatasetError("S3 finance object exceeds configured size limit")
        return raw
    try:
        with gzip.GzipFile(fileobj=BytesIO(raw)) as archive:
            decoded = archive.read(maximum + 1)
    except (OSError, EOFError) as error:
        raise S3FinanceDatasetError("S3 finance object is not valid gzip data") from error
    if len(decoded) > maximum:
        raise S3FinanceDatasetError(
            "Decompressed S3 finance object exceeds configured size limit"
        )
    return decoded


def _parse_json_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise S3FinanceDatasetError("S3 finance object is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise S3FinanceDatasetError("S3 finance JSON root must be an object")
    return payload
