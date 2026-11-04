"""Validated, atomic publication of dashboard datasets.

The SFTP service writes a temporary file and renames it to the final incoming
name.  This module only reads those final names.  A rejected upload never
replaces the currently published dataset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from threading import Lock
from typing import Any

from .config import DashboardSettings
from .constants import Audience, CompanyId
from .validation import DatasetValidationError, validate_dataset


logger = logging.getLogger("worker_dashboard.storage")
TARGETS: tuple[tuple[Audience, CompanyId], ...] = (
    ("worker", "tebo"),
    ("worker", "ssb"),
    ("finance", "tebo"),
    ("finance", "ssb"),
)


@dataclass
class PublishSummary:
    published: int = 0
    unchanged: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


class DatasetStore:
    def __init__(self, settings: DashboardSettings):
        self.settings = settings
        self._lock = Lock()

    def prepare_directories(self) -> None:
        for root in ("incoming", "published", "archive", "state"):
            (self.settings.data_root / root).mkdir(parents=True, exist_ok=True)
        for audience, _ in TARGETS:
            for root in ("incoming", "published", "archive"):
                (self.settings.data_root / root / audience).mkdir(
                    parents=True, exist_ok=True
                )

    def incoming_path(self, audience: Audience, company: CompanyId) -> Path:
        return self.settings.data_root / "incoming" / audience / f"{company}.json"

    def published_path(self, audience: Audience, company: CompanyId) -> Path:
        return self.settings.data_root / "published" / audience / f"{company}.json"

    def state_path(self, audience: Audience, company: CompanyId) -> Path:
        return self.settings.data_root / "state" / f"{audience}-{company}.json"

    def read(self, audience: Audience, company: CompanyId) -> bytes:
        published = self.published_path(audience, company)
        if self.settings.environment == "sample" and not published.is_file():
            path = self.settings.bundled_samples / f"{company}-{audience}.json"
        else:
            path = published
        if not path.is_file():
            raise FileNotFoundError(f"No published {company} {audience} dataset")
        size = path.stat().st_size
        if size > self.settings.max_dataset_bytes:
            raise DatasetValidationError("published dataset exceeds configured size limit")
        return path.read_bytes()

    def status(self, audience: Audience, company: CompanyId) -> dict[str, Any]:
        path = self.state_path(audience, company)
        if not path.is_file():
            return {
                "audience": audience,
                "company": company,
                "status": "sample" if self.settings.environment == "sample" else "pending",
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"audience": audience, "company": company, "status": "unknown"}
        return payload

    def scan_and_publish(self) -> PublishSummary:
        summary = PublishSummary()
        with self._lock:
            self.prepare_directories()
            for audience, company in TARGETS:
                source = self.incoming_path(audience, company)
                if not source.is_file():
                    continue
                if source.is_symlink():
                    summary.rejected += 1
                    summary.errors.append(f"{audience}/{company}: symbolic links are rejected")
                    continue
                try:
                    result = self._publish_one(audience, company, source)
                    if result == "published":
                        summary.published += 1
                    else:
                        summary.unchanged += 1
                except Exception as exc:
                    summary.rejected += 1
                    message = f"{audience}/{company}: {exc}"
                    summary.errors.append(message)
                    self._record_failure(audience, company, source, str(exc))
                    logger.warning("Dashboard dataset rejected: %s", message)
        return summary

    def _publish_one(
        self, audience: Audience, company: CompanyId, source: Path
    ) -> str:
        raw = _read_limited(source, self.settings.max_dataset_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        state = self.status(audience, company)
        if state.get("incoming_sha256") == digest and state.get("status") == "published":
            return "unchanged"

        validated = validate_dataset(
            raw,
            expected_audience=audience,
            expected_company=company,
            allow_sample=self.settings.environment == "sample",
        )
        target = self.published_path(audience, company)
        if target.is_file():
            current = validate_dataset(
                _read_limited(target, self.settings.max_dataset_bytes),
                expected_audience=audience,
                expected_company=company,
                allow_sample=self.settings.environment == "sample",
            )
            if current["source"]["asOf"] > validated["source"]["asOf"]:
                raise DatasetValidationError(
                    "incoming dataset is older than the currently published dataset"
                )
            self._archive_current(audience, company, target)

        canonical = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write(target, canonical)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write_state(
            audience,
            company,
            {
                "audience": audience,
                "company": company,
                "status": "published",
                "incoming_sha256": digest,
                "published_sha256": hashlib.sha256(canonical).hexdigest(),
                "source_as_of": validated["source"]["asOf"],
                "source_exported_at": validated["source"]["exportedAt"],
                "published_at": now,
                "last_error": None,
            },
        )
        logger.info(
            "Published dashboard dataset audience=%s company=%s source_as_of=%s",
            audience,
            company,
            validated["source"]["asOf"],
        )
        return "published"

    def _archive_current(
        self, audience: Audience, company: CompanyId, target: Path
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_dir = self.settings.data_root / "archive" / audience
        archive = archive_dir / f"{company}-{stamp}.json"
        shutil.copy2(target, archive)
        archives = sorted(
            archive_dir.glob(f"{company}-*.json"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for expired in archives[self.settings.archive_limit :]:
            expired.unlink(missing_ok=True)

    def _record_failure(
        self,
        audience: Audience,
        company: CompanyId,
        source: Path,
        error: str,
    ) -> None:
        previous = self.status(audience, company)
        digest = None
        try:
            digest = hashlib.sha256(
                _read_limited(source, self.settings.max_dataset_bytes)
            ).hexdigest()
        except Exception:
            pass
        previous.update(
            {
                "audience": audience,
                "company": company,
                "status": "rejected",
                "rejected_sha256": digest,
                "last_error": error[:1000],
                "last_attempt_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        self._write_state(audience, company, previous)

    def _write_state(
        self, audience: Audience, company: CompanyId, payload: dict[str, Any]
    ) -> None:
        _atomic_write(
            self.state_path(audience, company),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )


def _read_limited(path: Path, maximum: int) -> bytes:
    size = path.stat().st_size
    if size > maximum:
        raise DatasetValidationError("dataset exceeds configured size limit")
    with path.open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise DatasetValidationError("dataset exceeds configured size limit")
    return raw


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
