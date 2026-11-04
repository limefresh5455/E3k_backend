"""Bearer-token sessions backed by the existing hardened auth service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app import config as app_config
from app.finance_auth import service as auth_service
from app.finance_auth.errors import (
    FinanceAuthConfigurationError,
    InvalidCredentialsError,
)

from .access import (
    DashboardPrincipal,
    principal_for_user,
    require_dashboard_principal,
    set_private_response_headers,
    validate_browser_origin,
)
from .schemas import (
    DashboardLoginRequest,
    DashboardSessionResponse,
    DashboardTokenResponse,
    DashboardUserResponse,
    EntitlementInput,
)


router = APIRouter(
    prefix="/api/worker-dashboard/auth",
    tags=["worker-dashboard-auth"],
    dependencies=[Depends(set_private_response_headers)],
)


def _request_metadata(request: Request) -> tuple[str | None, str | None]:
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return ip_address, user_agent[:512] if user_agent else None


def _user_response(principal: DashboardPrincipal) -> DashboardUserResponse:
    return DashboardUserResponse(
        id=principal.id,
        email=principal.email,
        display_name=principal.display_name,
        is_active=True,
        entitlements=[
            EntitlementInput(permission=permission, company_scope=scope)
            for permission, scope in sorted(principal.entitlements)
        ],
    )


def _token_response(
    tokens: auth_service.AuthTokens,
    principal: DashboardPrincipal,
) -> DashboardTokenResponse:
    return DashboardTokenResponse(
        access_token=tokens.access.value,
        expires_in=app_config.WORKER_DASHBOARD_ACCESS_TOKEN_MINUTES * 60,
        user=_user_response(principal),
    )


def _authenticate(
    body: DashboardLoginRequest,
    request: Request,
) -> tuple[auth_service.AuthTokens, DashboardPrincipal]:
    validate_browser_origin(request)
    ip_address, user_agent = _request_metadata(request)
    try:
        tokens = auth_service.authenticate(
            str(body.email),
            body.password,
            ip_address=ip_address,
            user_agent=user_agent,
            issue_refresh_session=False,
            access_token_minutes=app_config.WORKER_DASHBOARD_ACCESS_TOKEN_MINUTES,
        )
        principal = principal_for_user(tokens.user)
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=401, detail="Invalid email or password") from exc
    return tokens, principal


@router.post("/login", response_model=DashboardTokenResponse)
def login(
    body: DashboardLoginRequest,
    request: Request,
):
    tokens, principal = _authenticate(body, request)
    if not principal.entitlements:
        raise HTTPException(status_code=403, detail="No dashboard access assigned")
    return _token_response(tokens, principal)


@router.get("/me", response_model=DashboardSessionResponse)
def me(principal: DashboardPrincipal = Depends(require_dashboard_principal)):
    return DashboardSessionResponse(user=_user_response(principal))
