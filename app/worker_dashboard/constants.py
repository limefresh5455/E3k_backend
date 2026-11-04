"""Shared authorization and company constants."""

from typing import Literal


CompanyId = Literal["tebo", "ssb"]
Audience = Literal["worker", "finance"]

COMPANIES: dict[str, dict[str, str | int]] = {
    "tebo": {
        "name": "Tebo AG",
        "client_id": "001",
        "fiscal_year_start_month": 7,
    },
    "ssb": {
        "name": "Schlauchservice Baumann GmbH",
        "client_id": "000",
        "fiscal_year_start_month": 1,
    },
}

WORKER_READ = "worker.read"
FINANCE_READ = "finance.read"
USERS_MANAGE = "users.manage"
PERMISSIONS = frozenset({WORKER_READ, FINANCE_READ, USERS_MANAGE})
COMPANY_SCOPES = frozenset(COMPANIES)
GLOBAL_SCOPE = "*"
