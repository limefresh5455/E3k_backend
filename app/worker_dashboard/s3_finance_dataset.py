"""Read, verify and validate finance datasets from S3.

Finance counterpart of ``s3_dataset.S3WorkerDatasetStore``.  The worker loader
cannot be reused for finance because it expands the compact
``europa3000-workers-v2`` export, whereas the finance object is checked
against the ``FinanceDataset`` dashboard contract.

Download, checksum verification, incremental parsing and the cache shared
between Gunicorn workers come from ``s3_cache``.

Manifest entry used: ``objects["finance/<company>"]`` (same entry that
``s3_api.py`` already serves through ``/api/s3-data/finance/<company>``).
"""

from __future__ import annotations

import os
from typing import Any

from .s3_cache import S3DatasetStore
from .s3_dataset import S3WorkerDatasetError
from .validation import DatasetValidationError, validate_dataset


# Like the worker loader, S3 is the source of truth for finance data, and the
# finance objects currently in the bucket may still be flagged as sample data
# (isSample=true / status "sample-only").  Allowed by default so they are served;
# set WORKER_DASHBOARD_S3_FINANCE_ALLOW_SAMPLE=false once real data is published.
_ALLOW_SAMPLE = os.getenv(
    "WORKER_DASHBOARD_S3_FINANCE_ALLOW_SAMPLE", "true"
).strip().lower() in {"1", "true", "yes", "on"}


class S3FinanceDatasetError(S3WorkerDatasetError):
    """Raised when the selected finance S3 generation is unavailable or invalid.

    Subclasses S3WorkerDatasetError so existing ``except`` clauses in the
    router handle it without changes.
    """


class S3FinanceDatasetStore(S3DatasetStore):
    audience = "finance"
    manifest_prefix = "finance"
    error_cls = S3FinanceDatasetError
    gzip_mode = "auto"  # gzip like the worker objects, or plain JSON

    def _validate_key(self, key: str, company: str) -> None:
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

    def _build_payload(
        self,
        source: dict[str, Any],
        *,
        company: str,
        entry: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            validated = validate_dataset(
                source,
                expected_audience="finance",
                expected_company=company,  # type: ignore[arg-type]
                allow_sample=_ALLOW_SAMPLE,
            )
        except DatasetValidationError as error:
            raise S3FinanceDatasetError(
                f"S3 finance dataset does not satisfy dashboard contract: {error} "
                f"(top-level keys: {sorted(source)[:12]}, "
                f"format={source.get('format')!r}, "
                f"schemaVersion={source.get('schemaVersion')!r})"
            ) from error
        finally:
            source.clear()  # release the parsed source; only the validated copy remains
        return validated
