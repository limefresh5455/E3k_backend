"""Authentication bridge and resource-level authorization."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config as app_config
from app.finance_auth.errors import FinanceAuthConfigurationError, InvalidTokenError
from app.finance_auth.service import authenticated_user_from_access_token

from . import repository
from .config import DashboardSettings
from .constants import FINANCE_READ, USERS_MANAGE, WORKER_READ


_bearer = HTTPBearer(auto_error=False)


def set_private_response_headers(response: Response) -> None:
    """Prevent authenticated dashboard responses from shared/browser caching."""
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"


def validate_browser_origin(request: Request) -> None:
    """Reject cross-origin cookie requests that can mutate server state."""
    origin = request.headers.get("origin")
    if not origin or "*" in app_config.CORS_ALLOWED_ORIGINS:
        return
    if origin.rstrip("/") not in app_config.CORS_ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Untrusted request origin")


@dataclass(frozen=True)
class DashboardPrincipal:
    id: int
    email: str
    display_name: str
    entitlements: frozenset[tuple[str, str]]

    def can_read(self, audience: str, company: str) -> bool:
        if audience == "finance":
            return (FINANCE_READ, company) in self.entitlements
        if audience == "worker":
            return (
                (WORKER_READ, company) in self.entitlements
                or (FINANCE_READ, company) in self.entitlements
            )
        return False

    @property
    def can_manage_users(self) -> bool:
        return (USERS_MANAGE, "*") in self.entitlements

    @property
    def companies(self) -> tuple[str, ...]:
        return tuple(
            company
            for company in ("tebo", "ssb")
            if self.can_read("worker", company)
        )


def get_settings() -> DashboardSettings:
    return DashboardSettings.from_env()


def principal_for_user(user: dict) -> DashboardPrincipal:
    display_name, entitlements = repository.get_profile_and_entitlements(user["id"])
    return DashboardPrincipal(
        id=user["id"],
        email=user["email"],
        display_name=display_name or user["email"],
        entitlements=frozenset(entitlements),
    )


def require_dashboard_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> DashboardPrincipal:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid dashboard bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user = authenticated_user_from_access_token(
            credentials.credentials,
            expected_dashboard_access="worker",
        )
        principal = principal_for_user(user)
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired dashboard session",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if not principal.entitlements:
        raise HTTPException(status_code=403, detail="No dashboard access assigned")
    return principal


def require_dashboard_admin(
    principal: DashboardPrincipal = Depends(require_dashboard_principal),
) -> DashboardPrincipal:
    if not principal.can_manage_users:
        raise HTTPException(status_code=403, detail="Dashboard administrator access required")
    return principal


def require_resource_access(
    principal: DashboardPrincipal,
    *,
    audience: str,
    company: str,
) -> None:
    if not principal.can_read(audience, company):
        raise HTTPException(status_code=403, detail="Dashboard access is not assigned")
