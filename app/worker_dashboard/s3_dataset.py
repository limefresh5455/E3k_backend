"""Read, verify and normalize compact worker datasets from S3."""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import hmac
from io import BytesIO
import json
import math
from threading import Lock
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import DashboardSettings
from .constants import COMPANIES
from .models import WorkerDataset


MANIFEST_MAX_BYTES = 1024 * 1024
WORKER_FORMAT = "europa3000-workers-v2"
_CACHE_LOCK = Lock()
_CACHE: dict[tuple[str, str, str], bytes] = {}


class S3WorkerDatasetError(RuntimeError):
    """Raised when the selected S3 generation is unavailable or invalid."""


class S3WorkerDatasetStore:
    def __init__(
        self,
        settings: DashboardSettings,
        s3_client=None,
    ):
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
        manifest, entry = self._manifest_entry(company)
        object_key = _required_text(entry, "key")
        expected_digest = _required_sha256(entry.get("sha256"))
        _validate_object_key(object_key, company)

        cache_key = (self.settings.s3_bucket, object_key, expected_digest)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached

        response = self._get_object(object_key, "worker object")
        compressed = _read_stream_limited(
            response.get("Body"),
            self.settings.max_dataset_bytes,
            "S3 worker object",
        )
        actual_digest = hashlib.sha256(compressed).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise S3WorkerDatasetError("S3 worker object checksum does not match manifest")

        decoded = _decompress_limited(
            compressed,
            self.settings.max_dataset_bytes,
        )
        source = _parse_object(decoded)
        normalized = normalize_worker_dataset(
            source,
            company=company,
            source_fetched_at=_required_text(entry, "sourceFetchedAt"),
            published_at=_optional_text(manifest.get("publishedAt")),
        )
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        with _CACHE_LOCK:
            # Four entries cover worker datasets for both configured companies
            # across a rolling generation without allowing unbounded growth.
            if len(_CACHE) >= 4:
                _CACHE.clear()
            _CACHE[cache_key] = canonical
        return canonical

    def status(self, company: str) -> dict[str, Any]:
        payload = json.loads(self.read(company))
        source = payload["source"]
        return {
            "audience": "worker",
            "company": company,
            "status": source["status"],
            "source_as_of": source["asOf"],
            "source_exported_at": source["exportedAt"],
        }

    def _manifest_entry(self, company: str) -> tuple[dict[str, Any], dict[str, Any]]:
        response = self._get_object(self.settings.s3_manifest_key, "manifest")
        raw = _read_stream_limited(
            response.get("Body"),
            MANIFEST_MAX_BYTES,
            "S3 manifest",
        )
        try:
            manifest = json.loads(raw.decode("utf-8"))
            entry = manifest["objects"][f"workers/{company}"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise S3WorkerDatasetError(
                f"S3 manifest has no valid workers/{company} entry"
            ) from error
        if not isinstance(manifest, dict) or not isinstance(entry, dict):
            raise S3WorkerDatasetError("S3 manifest worker entry is invalid")
        return manifest, entry

    def _get_object(self, key: str, label: str) -> dict[str, Any]:
        try:
            return self.s3.get_object(
                Bucket=self.settings.s3_bucket,
                Key=key,
            )
        except (BotoCoreError, ClientError) as error:
            raise S3WorkerDatasetError(
                f"Could not read S3 dashboard {label}"
            ) from error


def normalize_worker_dataset(
    source: dict[str, Any],
    *,
    company: str,
    source_fetched_at: str,
    published_at: str | None,
) -> dict[str, Any]:
    """Expand the verified v2 worker export into the dashboard API contract."""

    if company not in COMPANIES:
        raise S3WorkerDatasetError("Unsupported dashboard company")
    expected = COMPANIES[company]
    if source.get("format") != WORKER_FORMAT:
        raise S3WorkerDatasetError("Unexpected S3 worker format")
    if _text(source.get("client")) != expected["client_id"]:
        raise S3WorkerDatasetError("S3 worker client does not match requested company")
    if _text(source.get("company")) != expected["name"]:
        raise S3WorkerDatasetError("S3 worker company does not match requested company")

    customers_source = _rows(source, "customers")
    products_source = _rows(source, "products")
    groups_source = _rows(source, "groups")
    warehouses_source = _rows(source, "warehouses")
    articles_source = _objects(source, "articles")
    sales_source = _rows(source, "salesQuantities")
    documents_source = _rows(source, "customerDocuments")
    movements_source = _rows(source, "movements")
    as_of_date = _date(source.get("asof"))
    history_from = _date(source.get("cutoff"))

    customers = [
        {
            "id": _customer_id(index),
            "code": _nonempty(row[0], f"customer-{index}"),
            "name": _nonempty(row[1], _nonempty(row[0], f"Customer {index}")),
        }
        for index, row in enumerate(customers_source)
        if _require_width(row, 2, "customer")
    ]

    group_names = {
        _text(row[0]): _nonempty(row[1], _text(row[0]))
        for row in groups_source
        if _require_width(row, 2, "group")
    }
    warehouse_names = [
        _nonempty(row[0], _nonempty(row[1], f"warehouse-{index}"))
        for index, row in enumerate(warehouses_source)
        if _require_width(row, 2, "warehouse")
    ]
    master_by_number = {
        _text(article.get("number")): article
        for article in articles_source
        if _text(article.get("number"))
    }
    historical_units: dict[int, str] = {}
    for row in sales_source:
        _require_width(row, 7, "sales quantity")
        product_index = _index(row[2], len(products_source), "sales product")
        unit = _text(row[6])
        if unit:
            historical_units.setdefault(product_index, unit)

    articles: list[dict[str, Any]] = []
    article_units: dict[int, str] = {}
    product_numbers: set[str] = set()
    for index, row in enumerate(products_source):
        _require_width(row, 2, "product")
        number = _nonempty(row[0], f"product-{index}")
        product_numbers.add(number)
        master = master_by_number.get(number, {})
        unit = _nonempty(master.get("unit"), historical_units.get(index, "unit"))
        article_units[index] = unit
        group_code = _text(master.get("group"))
        articles.append(
            {
                "id": _article_id(index),
                "code": number,
                "name": _nonempty(row[1], _nonempty(master.get("name"), number)),
                "productGroup": _nonempty(
                    group_names.get(group_code),
                    group_code or "Unassigned",
                ),
                "unit": unit,
                "currentStock": _number(master.get("quantity", 0)),
                "minStock": _number(master.get("minimum", 0)),
                "orderedQty": _number(master.get("ordered", 0)),
                "reservedQty": _number(master.get("orderQuantity", 0)),
            }
        )

    # Current articles that have never appeared in historical product lookups
    # must still be visible in the stock list.
    for article in articles_source:
        number = _text(article.get("number"))
        if not number or number in product_numbers:
            continue
        group_code = _text(article.get("group"))
        articles.append(
            {
                "id": f"article-master:{_nonempty(article.get('id'), number)}",
                "code": number,
                "name": _nonempty(article.get("name"), number),
                "productGroup": _nonempty(
                    group_names.get(group_code),
                    group_code or "Unassigned",
                ),
                "unit": _nonempty(article.get("unit"), "unit"),
                "currentStock": _number(article.get("quantity", 0)),
                "minStock": _number(article.get("minimum", 0)),
                "orderedQty": _number(article.get("ordered", 0)),
                "reservedQty": _number(article.get("orderQuantity", 0)),
            }
        )

    sales_quantities = []
    for row in sales_source:
        _require_width(row, 7, "sales quantity")
        row_date = _date(row[0])
        if not history_from <= row_date <= as_of_date:
            continue
        customer_index = _index(row[1], len(customers), "sales customer")
        product_index = _index(row[2], len(products_source), "sales product")
        sales_quantities.append(
            {
                "date": row_date,
                "articleId": _article_id(product_index),
                "customerId": _customer_id(customer_index),
                "quantity": _number(row[4]),
                "unit": _nonempty(row[6], article_units[product_index]),
                "documentId": None,
            }
        )

    documents = []
    for row in documents_source:
        _require_width(row, 7, "customer document")
        row_date = _date(row[0])
        if not history_from <= row_date <= as_of_date:
            continue
        customer_index = _index(row[2], len(customers), "document customer")
        raw_id = _nonempty(row[6], _nonempty(row[1], "unknown"))
        raw_status = "/".join(
            part for part in (_text(row[4]), _text(row[5])) if part
        )
        documents.append(
            {
                "id": f"document:{raw_id}",
                "type": _document_type(row[3]),
                "number": _nonempty(row[1], raw_id),
                "date": row_date,
                "customerId": _customer_id(customer_index),
                "status": raw_status or "unknown",
                "pdfUrl": None,
            }
        )

    warehouse_movements = []
    for row in movements_source:
        _require_width(row, 7, "warehouse movement")
        row_date = _date(row[0])
        if not history_from <= row_date <= as_of_date:
            continue
        product_index = _index(row[1], len(products_source), "movement product")
        warehouse_index = _index(
            row[2], len(warehouse_names), "movement warehouse"
        )
        raw_id = _nonempty(row[5], "unknown")
        net_quantity = _number(row[3]) - _number(row[4])
        warehouse_movements.append(
            {
                "id": f"movement:{raw_id}",
                "date": row_date,
                "articleId": _article_id(product_index),
                "warehouse": warehouse_names[warehouse_index],
                "inQty": max(net_quantity, 0),
                "outQty": max(-net_quantity, 0),
                "unit": article_units[product_index],
                "documentId": None,
            }
        )

    exported_at = _utc_timestamp(source_fetched_at or published_at)
    normalized = {
        "schemaVersion": "1.0",
        "audience": "worker",
        "isSample": False,
        "company": {
            "id": company,
            "name": expected["name"],
            "clientId": expected["client_id"],
            "fiscalYearStartMonth": expected["fiscal_year_start_month"],
        },
        "source": {
            "asOf": f"{as_of_date}T00:00:00Z",
            "exportedAt": exported_at,
            "historyFrom": history_from,
            "status": "live",
        },
        "customers": customers,
        "articles": articles,
        "salesQuantities": sales_quantities,
        "documents": documents,
        "warehouseMovements": warehouse_movements,
    }
    try:
        return WorkerDataset.model_validate(normalized).model_dump(mode="json")
    except Exception as error:
        raise S3WorkerDatasetError(
            f"S3 worker dataset does not satisfy dashboard contract: {error}"
        ) from error


def _read_stream_limited(body, maximum: int, label: str) -> bytes:
    if body is None or not hasattr(body, "read"):
        raise S3WorkerDatasetError(f"{label} has no readable body")
    try:
        raw = body.read(maximum + 1)
    except (BotoCoreError, OSError) as error:
        raise S3WorkerDatasetError(f"Could not read {label} body") from error
    if len(raw) > maximum:
        raise S3WorkerDatasetError(f"{label} exceeds configured size limit")
    return raw


def _decompress_limited(compressed: bytes, maximum: int) -> bytes:
    if not compressed.startswith(b"\x1f\x8b"):
        raise S3WorkerDatasetError("S3 worker object is not gzip encoded")
    try:
        with gzip.GzipFile(fileobj=BytesIO(compressed)) as archive:
            decoded = archive.read(maximum + 1)
    except (OSError, EOFError) as error:
        raise S3WorkerDatasetError("S3 worker object is not valid gzip data") from error
    if len(decoded) > maximum:
        raise S3WorkerDatasetError(
            "Decompressed S3 worker object exceeds configured size limit"
        )
    return decoded


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise S3WorkerDatasetError("S3 worker object is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise S3WorkerDatasetError("S3 worker JSON root must be an object")
    return payload


def _validate_object_key(key: str, company: str) -> None:
    expected_suffix = f"/workers/{company}.json.gz"
    if not key.startswith("generations/") or ".." in key or not key.endswith(expected_suffix):
        raise S3WorkerDatasetError("S3 manifest contains an unexpected worker key")


def _required_sha256(value: Any) -> str:
    text = _text(value).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise S3WorkerDatasetError("S3 manifest contains an invalid SHA-256 value")
    return text


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = _text(mapping.get(key))
    if not value:
        raise S3WorkerDatasetError(f"S3 manifest entry is missing {key}")
    return value


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _rows(source: dict[str, Any], key: str) -> list[list[Any]]:
    value = source.get(key)
    if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
        raise S3WorkerDatasetError(f"S3 worker {key} must be a list of rows")
    return value


def _objects(source: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = source.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise S3WorkerDatasetError(f"S3 worker {key} must be a list of objects")
    return value


def _require_width(row: list[Any], width: int, label: str) -> bool:
    if len(row) != width:
        raise S3WorkerDatasetError(f"S3 worker {label} row must contain {width} values")
    return True


def _index(value: Any, length: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise S3WorkerDatasetError(f"S3 worker {label} index is invalid")
    return value


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise S3WorkerDatasetError("S3 worker numeric value is invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise S3WorkerDatasetError("S3 worker numeric value is not finite")
    return value


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _nonempty(value: Any, fallback: str) -> str:
    return _text(value) or fallback


def _date(value: Any) -> str:
    text = _text(value)
    if len(text) != 8 or not text.isdigit():
        raise S3WorkerDatasetError("S3 worker date must use YYYYMMDD")
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError as error:
        raise S3WorkerDatasetError("S3 worker date is invalid") from error
    return parsed.strftime("%Y-%m-%d")


def _utc_timestamp(value: str | None) -> str:
    if not value:
        raise S3WorkerDatasetError("S3 manifest has no source timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise S3WorkerDatasetError("S3 manifest source timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise S3WorkerDatasetError("S3 manifest source timestamp has no timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _customer_id(index: int) -> str:
    return f"customer:{index}"


def _article_id(index: int) -> str:
    return f"article:{index}"


def _document_type(value: Any) -> str:
    return {
        "O": "quotation",
        "X": "order",
        "Y": "proforma",
        "L": "delivery_note",
        "F": "invoice",
        "G": "credit_note",
    }.get(_text(value).upper(), "other")
