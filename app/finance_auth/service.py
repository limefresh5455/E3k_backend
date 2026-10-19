import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app import config

from . import repository
from .email_sender import validate_email_configuration
from .errors import InvalidCredentialsError, InvalidTokenError
from .security import (
    IssuedToken,
    decode_token,
    generate_otp,
    hash_password,
    issue_access_token,
    issue_password_reset_token,
    issue_refresh_token,
    otp_digest,
    token_digest,
    utc_now,
    validate_finance_auth_configuration,
    verify_dummy_password,
    verify_otp_digest,
    verify_password,
)

logger = logging.getLogger("finance_auth.service")


@dataclass(frozen=True)
class AuthTokens:
    access: IssuedToken
    refresh: IssuedToken
    user: dict


def register_user(
    email: str,
    password: str,
    role: str,
    *,
    actor: dict,
    ip_address: str | None,
    user_agent: str | None,
) -> dict:
    password_hash = hash_password(password)
    user = repository.create_finance_user(email, password_hash, role)
    _audit(
        "user_registered",
        True,
        user_id=user["id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    logger.info(
        "Finanz-Cockpit user registered by admin user_id=%s, new_user_id=%s",
        actor["id"],
        user["id"],
    )
    return user


def _audit(
    event: str,
    success: bool,
    *,
    user_id: int | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    try:
        repository.write_audit_log(
            event=event,
            success=success,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:
        logger.exception("Unable to write Finanz-Cockpit authentication audit log")


def authenticate(
    email: str,
    password: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> AuthTokens:
    validate_finance_auth_configuration()
    now = utc_now()
    user = repository.get_user_by_email(email)
    if not user:
        verify_dummy_password(password)
        _audit(
            "login",
            False,
            user_id=None,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise InvalidCredentialsError("Invalid email or password")

    password_matches = verify_password(password, user["password_hash"])
    account_locked = user["locked_until"] is not None and user["locked_until"] > now
    if not user["is_active"] or account_locked or not password_matches:
        if user["is_active"] and not account_locked:
            repository.register_failed_login(
                user["id"],
                now=now,
                maximum_attempts=config.FINANCE_LOGIN_MAX_ATTEMPTS,
                locked_until=now
                + timedelta(minutes=config.FINANCE_LOGIN_LOCK_MINUTES),
            )
        _audit(
            "login",
            False,
            user_id=user["id"],
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise InvalidCredentialsError("Invalid email or password")

    repository.register_successful_login(user["id"], now)
    user["failed_login_attempts"] = 0
    user["locked_until"] = None
    access = issue_access_token(user)
    refresh = issue_refresh_token(user)
    repository.create_refresh_session(
        jti=refresh.jti,
        family_id=refresh.family_id or refresh.jti,
        user_id=user["id"],
        token_digest=token_digest(refresh.value),
        expires_at=refresh.expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    _audit(
        "login",
        True,
        user_id=user["id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return AuthTokens(access=access, refresh=refresh, user=user)


def refresh_tokens(
    refresh_token: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> AuthTokens:
    payload = decode_token(refresh_token, "refresh")
    family_id = UUID(str(payload["sid"]))
    replacement = issue_refresh_token(
        {
            "id": int(payload["sub"]),
            "email": payload["email"],
            "role": payload["role"],
            "token_version": int(payload["tv"]),
        },
        family_id=family_id,
    )
    now = utc_now()
    user = repository.rotate_refresh_session(
        current_jti=UUID(str(payload["jti"])),
        family_id=family_id,
        current_digest=token_digest(refresh_token),
        expected_token_version=int(payload["tv"]),
        new_jti=replacement.jti,
        new_digest=token_digest(replacement.value),
        new_expires_at=replacement.expires_at,
        now=now,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    access = issue_access_token(user)
    _audit(
        "refresh",
        True,
        user_id=user["id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return AuthTokens(access=access, refresh=replacement, user=user)


def logout(
    refresh_token: str | None,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    user_id = None
    if refresh_token:
        try:
            payload = decode_token(refresh_token, "refresh")
            user_id = int(payload["sub"])
            repository.revoke_refresh_session(
                UUID(str(payload["jti"])), token_digest(refresh_token), utc_now()
            )
        except InvalidTokenError:
            pass
    _audit(
        "logout",
        True,
        user_id=user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def logout_all(
    user: dict, *, ip_address: str | None, user_agent: str | None
) -> None:
    repository.revoke_all_sessions_and_increment_token_version(user["id"], utc_now())
    _audit(
        "logout_all",
        True,
        user_id=user["id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )


def request_password_reset(
    email: str, *, ip_address: str | None, user_agent: str | None
) -> tuple[str, str] | None:
    validate_finance_auth_configuration()
    validate_email_configuration()
    now = utc_now()
    challenge_id = uuid4()
    otp = generate_otp()
    result = repository.create_password_reset_challenge(
        email=email,
        challenge_id=challenge_id,
        otp_digest=otp_digest(challenge_id, otp),
        expires_at=now + timedelta(minutes=config.FINANCE_OTP_EXPIRE_MINUTES),
        attempts=config.FINANCE_OTP_MAX_ATTEMPTS,
        now=now,
        resend_cutoff=now - timedelta(seconds=config.FINANCE_OTP_RESEND_SECONDS),
        hourly_cutoff=now - timedelta(hours=1),
        max_hourly_requests=config.FINANCE_OTP_MAX_REQUESTS_PER_HOUR,
        requested_ip=ip_address,
    )
    user = result[0] if result else None
    created = bool(result and result[1])
    _audit(
        "password_reset_requested",
        created,
        user_id=user["id"] if user else None,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    if not created:
        return None
    return user["email"], otp


def verify_password_reset_otp(
    email: str,
    otp: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> IssuedToken:
    validate_finance_auth_configuration()
    user_id = None
    try:
        user, challenge_id = repository.consume_password_reset_otp(
            email=email,
            otp=otp,
            now=utc_now(),
            verify_digest=verify_otp_digest,
        )
        user_id = user["id"]
        token = issue_password_reset_token(user, challenge_id)
    except Exception:
        _audit(
            "password_reset_otp_verified",
            False,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise
    _audit(
        "password_reset_otp_verified",
        True,
        user_id=user_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return token


def reset_password(
    reset_token: str,
    new_password: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    payload = decode_token(reset_token, "password_reset")
    password_hash = hash_password(new_password)
    user = repository.reset_password(
        user_id=int(payload["sub"]),
        expected_token_version=int(payload["tv"]),
        password_hash=password_hash,
        now=utc_now(),
    )
    _audit(
        "password_reset_completed",
        True,
        user_id=user["id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )


def authenticated_user_from_access_token(access_token: str) -> dict:
    payload = decode_token(access_token, "access")
    user = repository.get_user_by_id(int(payload["sub"]))
    if (
        not user
        or not user["is_active"]
        or user["role"] not in {"finance_user", "finance_admin"}
        or int(user["token_version"]) != int(payload["tv"])
    ):
        raise InvalidTokenError("Invalid or expired token")
    return user
