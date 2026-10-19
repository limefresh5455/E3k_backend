from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from app import config
from app.finance_auth import service
from app.finance_auth.dependencies import require_finance_admin, require_finance_user
from app.finance_auth.email_sender import send_password_reset_otp_safely
from app.finance_auth.errors import (
    FinanceAuthConfigurationError,
    InvalidCredentialsError,
    InvalidOtpError,
    InvalidTokenError,
    PasswordPolicyError,
    UserAlreadyExistsError,
)
from app.finance_auth.schemas import (
    FinanceLoginRequest,
    FinanceTokenResponse,
    FinanceUserResponse,
    ForgotPasswordRequest,
    MessageResponse,
    ResetGrantResponse,
    ResetPasswordRequest,
    RegisterFinanceUserRequest,
    VerifyResetOtpRequest,
)

router = APIRouter(prefix="/api/finance/auth", tags=["finance-auth"])

_GENERIC_RESET_MESSAGE = (
    "If an active Finanz-Cockpit account exists for that email, a reset code "
    "has been sent."
)


@router.post(
    "/register",
    response_model=FinanceUserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    body: RegisterFinanceUserRequest,
    request: Request,
    admin: dict = Depends(require_finance_admin),
):
    ip_address, user_agent = _request_metadata(request)
    try:
        user = service.register_user(
            str(body.email),
            body.password,
            body.role,
            actor=admin,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UserAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FinanceUserResponse(id=user["id"], email=user["email"], role=user["role"])


def _request_metadata(request: Request) -> tuple[str | None, str | None]:
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    if user_agent:
        user_agent = user_agent[:512]
    return ip_address, user_agent


def _validate_cookie_request_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or "*" in config.CORS_ALLOWED_ORIGINS:
        return
    if origin.rstrip("/") not in config.CORS_ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Untrusted request origin")


def _set_refresh_cookie(response: Response, token: str) -> None:
    if config.FINANCE_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
        raise FinanceAuthConfigurationError(
            "FINANCE_COOKIE_SAMESITE must be lax, strict, or none"
        )
    if config.FINANCE_COOKIE_SAMESITE == "none" and not config.FINANCE_COOKIE_SECURE:
        raise FinanceAuthConfigurationError(
            "FINANCE_COOKIE_SECURE must be true when SameSite is none"
        )
    response.set_cookie(
        key=config.FINANCE_REFRESH_COOKIE_NAME,
        value=token,
        max_age=config.FINANCE_REFRESH_TOKEN_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=config.FINANCE_COOKIE_SECURE,
        samesite=config.FINANCE_COOKIE_SAMESITE,
        path="/api/finance/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.FINANCE_REFRESH_COOKIE_NAME,
        httponly=True,
        secure=config.FINANCE_COOKIE_SECURE,
        samesite=config.FINANCE_COOKIE_SAMESITE,
        path="/api/finance/auth",
    )


def _token_response(tokens: service.AuthTokens) -> FinanceTokenResponse:
    return FinanceTokenResponse(
        access_token=tokens.access.value,
        expires_in=config.FINANCE_ACCESS_TOKEN_MINUTES * 60,
        user=FinanceUserResponse(
            id=tokens.user["id"],
            email=tokens.user["email"],
            role=tokens.user["role"],
        ),
    )


@router.post("/login", response_model=FinanceTokenResponse)
def login(body: FinanceLoginRequest, request: Request, response: Response):
    ip_address, user_agent = _request_metadata(request)
    try:
        tokens = service.authenticate(
            str(body.email),
            body.password,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        _set_refresh_cookie(response, tokens.refresh.value)
        return _token_response(tokens)
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.post("/refresh", response_model=FinanceTokenResponse)
def refresh(request: Request, response: Response):
    _validate_cookie_request_origin(request)
    refresh_token = request.cookies.get(config.FINANCE_REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    ip_address, user_agent = _request_metadata(request)
    try:
        tokens = service.refresh_tokens(
            refresh_token,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        _set_refresh_cookie(response, tokens.refresh.value)
        return _token_response(tokens)
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTokenError:
        invalid_response = JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired refresh token"},
        )
        _clear_refresh_cookie(invalid_response)
        return invalid_response


@router.get("/me", response_model=FinanceUserResponse)
def me(user: dict = Depends(require_finance_user)):
    return FinanceUserResponse(id=user["id"], email=user["email"], role=user["role"])


@router.post("/logout", response_model=MessageResponse)
def logout(request: Request, response: Response):
    _validate_cookie_request_origin(request)
    refresh_token = request.cookies.get(config.FINANCE_REFRESH_COOKIE_NAME)
    ip_address, user_agent = _request_metadata(request)
    service.logout(
        refresh_token,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    _clear_refresh_cookie(response)
    return MessageResponse(message="Logged out successfully")


@router.post("/logout-all", response_model=MessageResponse)
def logout_all(
    request: Request,
    response: Response,
    user: dict = Depends(require_finance_user),
):
    _validate_cookie_request_origin(request)
    ip_address, user_agent = _request_metadata(request)
    service.logout_all(user, ip_address=ip_address, user_agent=user_agent)
    _clear_refresh_cookie(response)
    return MessageResponse(message="All Finanz-Cockpit sessions have been logged out")


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    ip_address, user_agent = _request_metadata(request)
    try:
        delivery = service.request_password_reset(
            str(body.email),
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if delivery:
        recipient, otp = delivery
        background_tasks.add_task(send_password_reset_otp_safely, recipient, otp)
    return MessageResponse(message=_GENERIC_RESET_MESSAGE)


@router.post("/verify-otp", response_model=ResetGrantResponse)
def verify_otp(body: VerifyResetOtpRequest, request: Request):
    ip_address, user_agent = _request_metadata(request)
    try:
        token = service.verify_password_reset_otp(
            str(body.email),
            body.otp,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return ResetGrantResponse(
            reset_token=token.value,
            expires_in=config.FINANCE_RESET_TOKEN_MINUTES * 60,
        )
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidOtpError as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset code") from exc


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(body: ResetPasswordRequest, request: Request):
    ip_address, user_agent = _request_metadata(request)
    try:
        service.reset_password(
            body.reset_token,
            body.new_password,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return MessageResponse(message="Password reset successfully")
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTokenError as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token") from exc
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
