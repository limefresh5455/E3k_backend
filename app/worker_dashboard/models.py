"""Strict contracts for data received from the five-minute export pipeline."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from .constants import COMPANIES


Number = StrictInt | StrictFloat


class StrictModel(BaseModel):
    # JSON represents dates as strings.  Numeric/boolean business fields use
    # StrictInt/StrictFloat/StrictBool below so they still cannot be coerced.
    model_config = ConfigDict(extra="forbid")


class Company(StrictModel):
    id: Literal["tebo", "ssb"]
    name: str = Field(min_length=1, max_length=250)
    clientId: str = Field(min_length=1, max_length=20)
    fiscalYearStartMonth: StrictInt = Field(ge=1, le=12)

    @model_validator(mode="after")
    def validate_company_binding(self) -> "Company":
        expected = COMPANIES[self.id]
        if self.clientId != expected["client_id"]:
            raise ValueError("company/client binding is invalid")
        if self.fiscalYearStartMonth != expected["fiscal_year_start_month"]:
            raise ValueError("company fiscal-year binding is invalid")
        return self


class Source(StrictModel):
    asOf: datetime
    exportedAt: datetime
    historyFrom: date
    status: str = Field(min_length=1, max_length=100)

    @field_validator("asOf", "exportedAt", mode="before")
    @classmethod
    def require_utc_timestamp(cls, value):
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("timestamp must be an ISO-8601 UTC value ending in Z")
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> "Source":
        if self.asOf.tzinfo is None or self.exportedAt.tzinfo is None:
            raise ValueError("source timestamps must include a timezone")
        if self.historyFrom > self.asOf.date():
            raise ValueError("historyFrom must not be after asOf")
        if self.exportedAt < self.asOf:
            raise ValueError("exportedAt must not be before asOf")
        return self


class Customer(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    code: str = Field(min_length=1, max_length=250)
    name: str = Field(min_length=1, max_length=1000)


class Article(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    code: str = Field(min_length=1, max_length=250)
    name: str = Field(min_length=1, max_length=1000)
    productGroup: str = Field(min_length=1, max_length=500)
    unit: str = Field(min_length=1, max_length=100)
    currentStock: Number
    minStock: Number
    orderedQty: Number
    reservedQty: Number


class SalesQuantity(StrictModel):
    date: date
    articleId: str = Field(min_length=1, max_length=250)
    customerId: str = Field(min_length=1, max_length=250)
    quantity: Number
    unit: str = Field(min_length=1, max_length=100)
    documentId: str | None = Field(default=None, max_length=250)


class Document(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    type: Literal[
        "quotation",
        "order",
        "proforma",
        "delivery_note",
        "invoice",
        "credit_note",
        "other",
    ]
    number: str = Field(min_length=1, max_length=250)
    date: date
    customerId: str = Field(min_length=1, max_length=250)
    status: str = Field(min_length=1, max_length=100)
    pdfUrl: str | None = Field(default=None, max_length=2000)


class WarehouseMovement(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    date: date
    articleId: str = Field(min_length=1, max_length=250)
    warehouse: str = Field(min_length=1, max_length=250)
    inQty: Number = Field(ge=0)
    outQty: Number = Field(ge=0)
    unit: str = Field(min_length=1, max_length=100)
    documentId: str | None = Field(default=None, max_length=250)


class WorkerDataset(StrictModel):
    schemaVersion: Literal["1.0"]
    audience: Literal["worker"]
    isSample: StrictBool
    company: Company
    source: Source
    customers: list[Customer] = Field(max_length=1_000_000)
    articles: list[Article] = Field(max_length=1_000_000)
    salesQuantities: list[SalesQuantity] = Field(max_length=1_000_000)
    documents: list[Document] = Field(max_length=1_000_000)
    warehouseMovements: list[WarehouseMovement] = Field(max_length=1_000_000)

    @model_validator(mode="after")
    def validate_references(self) -> "WorkerDataset":
        customers = _unique_ids(self.customers, "customer")
        articles = _unique_ids(self.articles, "article")
        documents = _unique_ids(self.documents, "document")
        _unique_ids(self.warehouseMovements, "warehouse movement")
        start, end = self.source.historyFrom, self.source.asOf.date()

        for row in self.documents:
            if row.customerId not in customers:
                raise ValueError("document references an unknown customer")
            if not start <= row.date <= end:
                raise ValueError("document is outside the declared history range")
            if row.pdfUrl and (
                not row.pdfUrl.startswith("/api/worker-dashboard/documents/")
                or "\\" in row.pdfUrl
            ):
                raise ValueError("PDF URL must use the protected dashboard document route")

        for row in self.salesQuantities:
            article = articles.get(row.articleId)
            if article is None:
                raise ValueError("sales quantity references an unknown article")
            # Europa preserves the unit used at transaction time. It can differ
            # from the article's current master-data unit and must not be lost.
            if row.customerId not in customers:
                raise ValueError("sales quantity references an unknown customer")
            if row.documentId is not None and row.documentId not in documents:
                raise ValueError("sales quantity references an unknown document")
            if not start <= row.date <= end:
                raise ValueError("sales quantity is outside the declared history range")

        for row in self.warehouseMovements:
            article = articles.get(row.articleId)
            if article is None or article.unit != row.unit:
                raise ValueError("warehouse movement has an invalid article/unit reference")
            if row.documentId is not None and row.documentId not in documents:
                raise ValueError("warehouse movement references an unknown document")
            if not start <= row.date <= end:
                raise ValueError("warehouse movement is outside the declared history range")
        return self


class MonthlyResult(StrictModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    netRevenueCents: StrictInt
    materialExpenseCents: StrictInt
    grossProfitCents: StrictInt
    personnelExpenseCents: StrictInt
    otherOperatingExpenseCents: StrictInt
    operatingProfitCents: StrictInt
    directTaxCents: StrictInt
    netProfitCents: StrictInt
    coverage: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reconcile(self) -> "MonthlyResult":
        if self.grossProfitCents != self.netRevenueCents - self.materialExpenseCents:
            raise ValueError("gross profit does not reconcile")
        expected_operating = (
            self.grossProfitCents
            - self.personnelExpenseCents
            - self.otherOperatingExpenseCents
        )
        if self.operatingProfitCents != expected_operating:
            raise ValueError("operating profit does not reconcile")
        if self.netProfitCents != self.operatingProfitCents - self.directTaxCents:
            raise ValueError("net profit does not reconcile")
        return self


class CashAccount(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    label: str = Field(min_length=1, max_length=500)
    currency: str = Field(min_length=3, max_length=3)
    balanceCents: StrictInt
    includeInLiquidity: StrictBool


class Cash(StrictModel):
    asOf: datetime
    basis: Literal["book-balance"]
    accounts: list[CashAccount] = Field(max_length=10_000)
    confirmed: StrictBool

    @field_validator("asOf", mode="before")
    @classmethod
    def require_utc_timestamp(cls, value):
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("cash timestamp must be an ISO-8601 UTC value ending in Z")
        return value


class OpenItem(StrictModel):
    id: str = Field(min_length=1, max_length=250)
    documentNumber: str = Field(min_length=1, max_length=250)
    counterparty: str = Field(min_length=1, max_length=1000)
    issueDate: date
    dueDate: date
    currency: str = Field(min_length=3, max_length=3)
    originalAmountCents: StrictInt
    outstandingAmountCents: StrictInt
    status: Literal["open", "paid", "partially_paid", "overdue"]
    paidDate: date | None


class InventoryValuation(StrictModel):
    status: str = Field(min_length=1, max_length=100)
    valueCents: StrictInt | None
    reason: str = Field(min_length=1, max_length=1000)


class FinanceDataset(StrictModel):
    schemaVersion: Literal["1.0"]
    audience: Literal["finance"]
    isSample: StrictBool
    company: Company
    source: Source
    currency: str = Field(min_length=3, max_length=3)
    amountUnit: Literal["integer-cents"]
    monthlyResults: list[MonthlyResult] = Field(max_length=10_000)
    cash: Cash
    receivables: list[OpenItem] = Field(max_length=1_000_000)
    payables: list[OpenItem] = Field(max_length=1_000_000)
    inventoryValuation: InventoryValuation


def _unique_ids(rows: list[object], label: str) -> dict[str, object]:
    found: dict[str, object] = {}
    for row in rows:
        row_id = getattr(row, "id")
        if row_id in found:
            raise ValueError(f"duplicate {label} identifier")
        found[row_id] = row
    return found
