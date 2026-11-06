"""Environment-backed settings for the Worker Dashboard integration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from app import config as app_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class DashboardSettings:
    """Runtime configuration with safe staging defaults."""

    environment: str
    data_root: Path
    publisher_enabled: bool
    publish_interval_seconds: int
    max_dataset_bytes: int
    archive_limit: int
    s3_bucket: str
    s3_region: str
    s3_manifest_key: str

    @classmethod
    def from_env(cls) -> "DashboardSettings":
        environment = os.getenv("WORKER_DASHBOARD_ENVIRONMENT", "sample").strip().lower()
        if environment not in {"sample", "production"}:
            raise ValueError(
                "WORKER_DASHBOARD_ENVIRONMENT must be sample or production"
            )
        configured_root = Path(
            os.getenv(
                "WORKER_DASHBOARD_DATA_ROOT",
                str(PROJECT_ROOT / "data" / "worker_dashboard"),
            )
        )
        if not configured_root.is_absolute():
            configured_root = PROJECT_ROOT / configured_root
        return cls(
            environment=environment,
            data_root=configured_root.resolve(),
            publisher_enabled=_boolean(
                "WORKER_DASHBOARD_PUBLISHER_ENABLED", environment == "production"
            ),
            publish_interval_seconds=_integer(
                "WORKER_DASHBOARD_PUBLISH_INTERVAL_SECONDS", 300, 60, 3600
            ),
            max_dataset_bytes=_integer(
                "WORKER_DASHBOARD_MAX_DATASET_BYTES", 25 * 1024 * 1024, 1024, 250 * 1024 * 1024
            ),
            archive_limit=_integer("WORKER_DASHBOARD_ARCHIVE_LIMIT", 24, 1, 500),
            s3_bucket=os.getenv("S3_BUCKET", "e3k-backend").strip(),
            s3_region=os.getenv("AWS_REGION", "eu-north-1").strip(),
            s3_manifest_key=os.getenv("S3_MANIFEST_KEY", "current.json").strip(),
        )

    @property
    def bundled_samples(self) -> Path:
        return PACKAGE_ROOT / "samples"

    def validate(self) -> None:
        if self.environment == "production" and "*" in app_config.CORS_ALLOWED_ORIGINS:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS must list trusted HTTPS origins in production"
            )
        if self.environment == "production" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required in production")
        if self.environment == "production" and not self.s3_region:
            raise ValueError("AWS_REGION is required in production")
        if self.environment == "production" and not self.s3_manifest_key:
            raise ValueError("S3_MANIFEST_KEY is required in production")
