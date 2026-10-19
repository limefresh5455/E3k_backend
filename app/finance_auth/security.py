import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app import config

from .errors import (
    FinanceAuthConfigurationError,
    InvalidTokenError,
    PasswordPolicyError,
)

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
_DUMMY_PASSWORD_HASH: str | None = None


@dataclass(frozen=True)
class IssuedToken:
    value: str
    jti: UUID
    expires_at: datetime
    family_id: UUID | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_password(password: str) -> None:
    if password != password.strip():
        raise PasswordPolicyError("Password must not start or end with whitespace")
    if len(password) < 12:
        raise PasswordPolicyError("Password must contain at least 12 characters")
    if len(password) > 128:
        raise PasswordPolicyError("Password must contain at most 128 characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_dummy_password(password: str) -> None:
    """Spend password-hashing work for unknown accounts to reduce enumeration."""
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(
            "not-a-real-finance-account-password"
        )
    verify_password(password, _DUMMY_PASSWORD_HASH)


def _require_secret(value: str, name: str) -> str:
    normalized = value.strip().casefold()
    if (
        len(value.encode("utf-8")) < 32
        or normalized.startswith(("replace-", "changeme", "change-me"))
    ):
        raise FinanceAuthConfigurationError(
            f"{name} must be configured with at least 32 bytes"
        )
    return value


def validate_finance_auth_configuration() -> None:
    access_secret = _require_secret(
        config.FINANCE_JWT_ACCESS_SECRET, "FINANCE_JWT_ACCESS_SECRET"
    )
    refresh_secret = _require_secret(
        config.FINANCE_JWT_REFRESH_SECRET, "FINANCE_JWT_REFRESH_SECRET"
    )
    otp_pepper = _require_secret(config.FINANCE_OTP_PEPPER, "FINANCE_OTP_PEPPER")
    if len({access_secret, refresh_secret, otp_pepper}) != 3:
        raise FinanceAuthConfigurationError(
            "Finanz JWT secrets and OTP pepper must use three different values"
        )


def _base_claims(user: dict, token_type: str, jti: UUID, expires_at: datetime) -> dict:
    now = utc_now()
    return {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user["role"],
        "type": token_type,
        "jti": str(jti),
        "tv": int(user["token_version"]),
        "iss": config.FINANCE_JWT_ISSUER,
        "aud": config.FINANCE_JWT_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": expires_at,
    }


def issue_access_token(user: dict) -> IssuedToken:
    secret = _require_secret(
        config.FINANCE_JWT_ACCESS_SECRET, "FINANCE_JWT_ACCESS_SECRET"
    )
    jti = uuid4()
    expires_at = utc_now() + timedelta(minutes=config.FINANCE_ACCESS_TOKEN_MINUTES)
    value = jwt.encode(
        _base_claims(user, "access", jti, expires_at),
        secret,
        algorithm=config.FINANCE_JWT_ALGORITHM,
    )
    return IssuedToken(value=value, jti=jti, expires_at=expires_at)


def issue_refresh_token(user: dict, family_id: UUID | None = None) -> IssuedToken:
    secret = _require_secret(
        config.FINANCE_JWT_REFRESH_SECRET, "FINANCE_JWT_REFRESH_SECRET"
    )
    jti = uuid4()
    family_id = family_id or jti
    expires_at = utc_now() + timedelta(days=config.FINANCE_REFRESH_TOKEN_DAYS)
    claims = _base_claims(user, "refresh", jti, expires_at)
    claims["sid"] = str(family_id)
    value = jwt.encode(
        claims,
        secret,
        algorithm=config.FINANCE_JWT_ALGORITHM,
    )
    return IssuedToken(
        value=value,
        jti=jti,
        expires_at=expires_at,
        family_id=family_id,
    )


def issue_password_reset_token(user: dict, challenge_id: UUID) -> IssuedToken:
    secret = _require_secret(
        config.FINANCE_JWT_ACCESS_SECRET, "FINANCE_JWT_ACCESS_SECRET"
    )
    jti = uuid4()
    expires_at = utc_now() + timedelta(minutes=config.FINANCE_RESET_TOKEN_MINUTES)
    claims = _base_claims(user, "password_reset", jti, expires_at)
    claims["cid"] = str(challenge_id)
    value = jwt.encode(
        claims,
        secret,
        algorithm=config.FINANCE_JWT_ALGORITHM,
    )
    return IssuedToken(value=value, jti=jti, expires_at=expires_at)


def decode_token(token: str, expected_type: str) -> dict:
    if expected_type == "refresh":
        secret = _require_secret(
            config.FINANCE_JWT_REFRESH_SECRET, "FINANCE_JWT_REFRESH_SECRET"
        )
        required_claims = [
            "sub",
            "email",
            "role",
            "type",
            "jti",
            "sid",
            "tv",
            "iat",
            "nbf",
            "exp",
        ]
    else:
        secret = _require_secret(
            config.FINANCE_JWT_ACCESS_SECRET, "FINANCE_JWT_ACCESS_SECRET"
        )
        required_claims = [
            "sub",
            "email",
            "role",
            "type",
            "jti",
            "tv",
            "iat",
            "nbf",
            "exp",
        ]
        if expected_type == "password_reset":
            required_claims.append("cid")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[config.FINANCE_JWT_ALGORITHM],
            issuer=config.FINANCE_JWT_ISSUER,
            audience=config.FINANCE_JWT_AUDIENCE,
            options={"require": required_claims},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid or expired token") from exc

    if payload.get("type") != expected_type:
        raise InvalidTokenError("Invalid token type")
    if payload.get("role") not in {"finance_user", "finance_admin"}:
        raise InvalidTokenError("Invalid finance role")
    try:
        int(payload["sub"])
        int(payload["tv"])
        UUID(str(payload["jti"]))
        if expected_type == "refresh":
            UUID(str(payload["sid"]))
        if expected_type == "password_reset":
            UUID(str(payload["cid"]))
    except (TypeError, ValueError) as exc:
        raise InvalidTokenError("Invalid token claims") from exc
    return payload


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_digest(challenge_id: UUID, otp: str) -> str:
    pepper = _require_secret(config.FINANCE_OTP_PEPPER, "FINANCE_OTP_PEPPER")
    message = f"{challenge_id}:{otp}".encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_otp_digest(challenge_id: UUID, otp: str, expected_digest: str) -> bool:
    return hmac.compare_digest(otp_digest(challenge_id, otp), expected_digest)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
