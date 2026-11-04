"""Dashboard user provisioning and manual publication controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.finance_auth import repository as auth_repository
from app.finance_auth import service as auth_service
from app.finance_auth.errors import PasswordPolicyError, UserAlreadyExistsError

from . import repository
from .access import (
    DashboardPrincipal,
    get_settings,
    require_dashboard_admin,
    set_private_response_headers,
    validate_browser_origin,
)
from .config import DashboardSettings
from .constants import USERS_MANAGE
from .database import DashboardPublicationAlreadyRunning, DashboardPublicationLock
from .schemas import (
    CreateDashboardUserRequest,
    DashboardUserResponse,
    EntitlementInput,
    PublishSummaryResponse,
    SetUserActiveRequest,
    UpdateDashboardUserRequest,
)
from .storage import DatasetStore


router = APIRouter(
    prefix="/api/worker-dashboard/admin",
    tags=["worker-dashboard-admin"],
    dependencies=[
        Depends(validate_browser_origin),
        Depends(set_private_response_headers),
    ],
)


def _pairs(items: list[EntitlementInput]) -> set[tuple[str, str]]:
    return {(item.permission, item.company_scope) for item in items}


def _user_response(user: dict) -> DashboardUserResponse:
    entitlements = user.get("entitlements", [])
    return DashboardUserResponse(
        id=user["id"],
        email=user["email"],
        display_name=user.get("display_name") or user["email"],
        is_active=user.get("is_active", True),
        entitlements=[
            EntitlementInput(
                permission=item["permission"],
                company_scope=item["company_scope"],
            )
            for item in entitlements
        ],
    )


@router.get("/users", response_model=list[DashboardUserResponse])
def list_users(_: DashboardPrincipal = Depends(require_dashboard_admin)):
    return [_user_response(user) for user in repository.list_dashboard_users()]


@router.post(
    "/users",
    response_model=DashboardUserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    body: CreateDashboardUserRequest,
    request: Request,
    admin: DashboardPrincipal = Depends(require_dashboard_admin),
):
    entitlements = _pairs(body.entitlements)
    underlying_role = (
        "finance_admin"
        if (USERS_MANAGE, "*") in entitlements
        else "finance_user"
    )
    ip_address = request.client.host if request.client else None
    try:
        user = auth_service.register_user(
            str(body.email),
            body.password,
            underlying_role,
            actor={"id": admin.id},
            ip_address=ip_address,
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
        repository.replace_user_access(
            user["id"],
            display_name=body.display_name,
            entitlements=entitlements,
        )
        repository.write_audit_log(
            actor_user_id=admin.id,
            target_user_id=user["id"],
            event="dashboard_user_created",
            details={
                "email": user["email"],
                "entitlements": sorted(entitlements),
            },
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return DashboardUserResponse(
        id=user["id"],
        email=user["email"],
        display_name=body.display_name,
        entitlements=body.entitlements,
        is_active=True,
    )


@router.put("/users/{user_id}", response_model=DashboardUserResponse)
def update_user(
    user_id: int,
    body: UpdateDashboardUserRequest,
    admin: DashboardPrincipal = Depends(require_dashboard_admin),
):
    user = auth_repository.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Dashboard user not found")
    entitlements = _pairs(body.entitlements)
    if user_id == admin.id and (USERS_MANAGE, "*") not in entitlements:
        raise HTTPException(
            status_code=409,
            detail="You cannot remove your own administrator permission",
        )
    repository.replace_user_access(
        user_id,
        display_name=body.display_name,
        entitlements=entitlements,
    )
    repository.write_audit_log(
        actor_user_id=admin.id,
        target_user_id=user_id,
        event="dashboard_access_updated",
        details={"entitlements": sorted(entitlements)},
    )
    return DashboardUserResponse(
        id=user_id,
        email=user["email"],
        display_name=body.display_name,
        entitlements=body.entitlements,
        is_active=user["is_active"],
    )


@router.put("/users/{user_id}/active", status_code=204)
def set_user_active(
    user_id: int,
    body: SetUserActiveRequest,
    admin: DashboardPrincipal = Depends(require_dashboard_admin),
):
    if user_id == admin.id and not body.active:
        raise HTTPException(status_code=409, detail="You cannot deactivate your own account")
    if not repository.set_user_active(user_id, body.active):
        raise HTTPException(status_code=404, detail="Dashboard user not found")
    repository.write_audit_log(
        actor_user_id=admin.id,
        target_user_id=user_id,
        event="dashboard_user_status_changed",
        details={"active": body.active},
    )


@router.post("/publish", response_model=PublishSummaryResponse)
def publish_now(
    _: DashboardPrincipal = Depends(require_dashboard_admin),
    settings: DashboardSettings = Depends(get_settings),
):
    try:
        with DashboardPublicationLock():
            summary = DatasetStore(settings).scan_and_publish()
    except DashboardPublicationAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PublishSummaryResponse(
        published=summary.published,
        unchanged=summary.unchanged,
        rejected=summary.rejected,
        errors=summary.errors,
    )
