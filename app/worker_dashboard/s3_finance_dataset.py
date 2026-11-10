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

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os
from typing import Any

from .constants import COMPANIES
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
            if not _looks_like_raw_finance_export(source):
                raise S3FinanceDatasetError(
                    f"S3 finance dataset does not satisfy dashboard contract: {error} "
                    f"(top-level keys: {sorted(source)[:12]}, "
                    f"format={source.get('format')!r}, "
                    f"schemaVersion={source.get('schemaVersion')!r})"
                ) from error
            normalized = normalize_finance_dataset(
                source,
                company=company,
                source_fetched_at=_text(entry.get("sourceFetchedAt")),
            )
            try:
                validated = validate_dataset(
                    normalized,
                    expected_audience="finance",
                    expected_company=company,  # type: ignore[arg-type]
                    allow_sample=_ALLOW_SAMPLE,
                )
            except DatasetValidationError as normalized_error:
                raise S3FinanceDatasetError(
                    "S3 raw finance export could not be normalized into the "
                    f"dashboard contract: {normalized_error}"
                ) from normalized_error
        finally:
            source.clear()  # release the parsed source; only the validated copy remains
        return validated


def normalize_finance_dataset(
    source: dict[str, Any],
    *,
    company: str,
    source_fetched_at: str,
) -> dict[str, Any]:
    """Convert the raw Europa finance export into the dashboard finance contract."""

    if company not in COMPANIES:
        raise S3FinanceDatasetError("Unsupported dashboard company")
    expected = COMPANIES[company]
    if _text(source.get("client")) != expected["client_id"]:
        raise S3FinanceDatasetError("S3 finance client does not match requested company")
    if _text(source.get("company")) != expected["name"]:
        raise S3FinanceDatasetError("S3 finance company does not match requested company")

    as_of_date = _date(source.get("asof"), "finance as-of date")
    history_from = _date(source.get("cutoff"), "finance cutoff date")
    as_of_timestamp = f"{as_of_date}T00:00:00Z"
    exported_at = _utc_timestamp(source_fetched_at, fallback=as_of_timestamp)

    customers = _rows(source, "customers")
    cash_accounts = _cash_accounts(source)
    receivables = _open_items(
        source,
        key="receivables",
        customers=customers,
        as_of_date=as_of_date,
    )
    payables = _open_items(
        source,
        key="payables",
        customers=customers,
        as_of_date=as_of_date,
    )

    return {
        "schemaVersion": "1.0",
        "audience": "finance",
        "isSample": False,
        "company": {
            "id": company,
            "name": expected["name"],
            "clientId": expected["client_id"],
            "fiscalYearStartMonth": expected["fiscal_year_start_month"],
        },
        "source": {
            "asOf": as_of_timestamp,
            "exportedAt": exported_at,
            "historyFrom": history_from,
            "status": _source_status(source),
        },
        "currency": "CHF",
        "amountUnit": "integer-cents",
        # The shared raw export has open-item and ledger balances for both
        # companies, but not a reliable monthly P&L source for SSB.
        "monthlyResults": [],
        "cash": {
            "asOf": as_of_timestamp,
            "basis": "book-balance",
            "accounts": cash_accounts,
            "confirmed": bool(_mapping(source.get("cashAccounts")).get("complete")),
        },
        "receivables": receivables,
        "payables": payables,
        "inventoryValuation": _inventory_valuation(source),
    }


def _looks_like_raw_finance_export(source: dict[str, Any]) -> bool:
    return (
        "schemaVersion" not in source
        and isinstance(source.get("cashAccounts"), dict)
        and isinstance(source.get("receivables"), list)
        and isinstance(source.get("payables"), list)
    )


def _cash_accounts(source: dict[str, Any]) -> list[dict[str, Any]]:
    cash = _mapping(source.get("cashAccounts"))
    accounts = cash.get("accounts")
    if not isinstance(accounts, list):
        raise S3FinanceDatasetError("S3 finance cashAccounts.accounts must be a list")
    defaults = _mapping(source.get("cashSelectionDefaults"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, account in enumerate(accounts):
        if not isinstance(account, dict):
            raise S3FinanceDatasetError("S3 finance cash account must be an object")
        number = _nonempty(account.get("Number"), f"account-{index}")
        account_id = number if number not in seen else f"{number}:{index}"
        seen.add(account_id)
        default = _mapping(defaults.get(number))
        result.append(
            {
                "id": f"account:{account_id}",
                "label": _nonempty(account.get("Title"), number),
                "currency": _currency(default.get("currency")),
                "balanceCents": _money_to_cents(account.get("BalanceCurrent")),
                "includeInLiquidity": bool(default.get("include", False)),
            }
        )
    return result


def _open_items(
    source: dict[str, Any],
    *,
    key: str,
    customers: list[list[Any]],
    as_of_date: str,
) -> list[dict[str, Any]]:
    rows = _rows(source, key)
    payment_details = _mapping(_mapping(source.get("paymentDetails")).get(key))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        _require_width(row, 10, key)
        raw_id = _nonempty(row[9], f"{key}-{index}")
        item_id = raw_id if raw_id not in seen else f"{raw_id}:{index}"
        seen.add(item_id)
        counterparty_index = _index(row[2], len(customers), f"{key} counterparty")
        payment = payment_details.get(raw_id)
        paid_date = _paid_date(payment)
        outstanding = _integer(row[4], f"{key} outstanding amount")
        issue_date = _date(row[0], f"{key} issue date")
        due_date = _optional_date(row[5], f"{key} due date") or issue_date
        result.append(
            {
                "id": f"{key}:{item_id}",
                "documentNumber": _nonempty(row[1], raw_id),
                "counterparty": _counterparty(customers[counterparty_index]),
                "issueDate": issue_date,
                "dueDate": due_date,
                "currency": _voucher_currency(row[8]),
                "originalAmountCents": _integer(row[3], f"{key} original amount"),
                "outstandingAmountCents": outstanding,
                "status": _open_item_status(
                    outstanding=outstanding,
                    paid_date=paid_date,
                    due_date=due_date,
                    as_of_date=as_of_date,
                ),
                "paidDate": paid_date,
            }
        )
    return result


def _inventory_valuation(source: dict[str, Any]) -> dict[str, Any]:
    accounts = _mapping(source.get("cashAccounts")).get("accounts")
    if not isinstance(accounts, list):
        return {
            "status": "not-approved",
            "valueCents": None,
            "reason": "No ledger account balances were available in the raw export.",
        }
    selected: list[tuple[str, int]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        number = _text(account.get("Number"))
        title = _text(account.get("Title")).lower()
        if number in {"1070", "1071", "1200"} or (
            number.startswith("1") and "warenlager" in title
        ):
            selected.append((number, _money_to_cents(account.get("BalanceCurrent"))))
    if not selected:
        return {
            "status": "not-approved",
            "valueCents": None,
            "reason": "No inventory ledger accounts were found in the raw export.",
        }
    return {
        "status": "ledger-balance",
        "valueCents": sum(value for _number, value in selected),
        "reason": (
            "Inventory valuation is derived from raw export ledger account "
            f"balances: {', '.join(number for number, _value in selected)}."
        ),
    }


def _source_status(source: dict[str, Any]) -> str:
    cash_complete = bool(_mapping(source.get("cashAccounts")).get("complete"))
    validation = _mapping(source.get("exportValidation"))
    if validation and validation.get("completeFlag") is False:
        return "live-partial"
    return "live" if cash_complete else "live-unconfirmed"


def _open_item_status(
    *,
    outstanding: int,
    paid_date: str | None,
    due_date: str,
    as_of_date: str,
) -> str:
    if outstanding <= 0:
        return "paid"
    if due_date < as_of_date:
        return "overdue"
    if paid_date is not None:
        return "partially_paid"
    return "open"


def _paid_date(payment: Any) -> str | None:
    if not isinstance(payment, list) or not payment:
        return None
    value = payment[0]
    if value in {None, "", 0, "0"}:
        return None
    return _date(value, "finance payment date")


def _counterparty(row: list[Any]) -> str:
    if len(row) < 2:
        raise S3FinanceDatasetError("S3 finance counterparty row is invalid")
    return _nonempty(row[1], _nonempty(row[0], "Unknown counterparty"))


def _rows(source: dict[str, Any], key: str) -> list[list[Any]]:
    value = source.get(key)
    if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
        raise S3FinanceDatasetError(f"S3 finance {key} must be a list of rows")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_width(row: list[Any], width: int, label: str) -> None:
    if len(row) != width:
        raise S3FinanceDatasetError(f"S3 finance {label} row must contain {width} values")


def _index(value: Any, length: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < length:
        raise S3FinanceDatasetError(f"S3 finance {label} index is invalid")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise S3FinanceDatasetError(f"S3 finance {label} must be an integer")
    return value


def _money_to_cents(value: Any) -> int:
    if isinstance(value, bool):
        raise S3FinanceDatasetError("S3 finance money value is invalid")
    try:
        amount = Decimal(str(0 if value is None else value))
    except (InvalidOperation, ValueError) as error:
        raise S3FinanceDatasetError("S3 finance money value is invalid") from error
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _currency(value: Any) -> str:
    text = _text(value).upper()
    return text if len(text) == 3 else "CHF"


def _voucher_currency(value: Any) -> str:
    return {
        "": "CHF",
        "00": "CHF",
        "01": "EUR",
        "02": "USD",
    }.get(_text(value), "CHF")


def _date(value: Any, label: str) -> str:
    text = _text(value)
    if len(text) != 8 or not text.isdigit():
        raise S3FinanceDatasetError(f"S3 {label} must use YYYYMMDD")
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError as error:
        raise S3FinanceDatasetError(f"S3 {label} is invalid") from error
    return parsed.strftime("%Y-%m-%d")


def _optional_date(value: Any, label: str) -> str | None:
    text = _text(value)
    if text in {"", "0"}:
        return None
    return _date(value, label)


def _utc_timestamp(value: str, *, fallback: str) -> str:
    text = _text(value)
    if not text:
        return fallback
    try:
        parsed = _parse_iso_datetime(text)
    except ValueError as error:
        raise S3FinanceDatasetError("S3 manifest source timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise S3FinanceDatasetError("S3 manifest source timestamp has no timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    date_time, sign, offset = text.partition("+")
    if not sign:
        date_time, sign, offset = text.partition("-") if "T" not in text else _split_negative_offset(text)
    if "." in date_time:
        head, fraction = date_time.split(".", 1)
        date_time = f"{head}.{fraction[:6]}"
    return datetime.fromisoformat(date_time + (sign + offset if sign else ""))


def _split_negative_offset(value: str) -> tuple[str, str, str]:
    marker = value.rfind("-")
    if marker > value.find("T"):
        return value[:marker], "-", value[marker + 1 :]
    return value, "", ""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _nonempty(value: Any, fallback: str) -> str:
    return _text(value) or fallback
