import hmac
from contextlib import closing
from datetime import datetime
from typing import Callable
from uuid import UUID

import psycopg2
from psycopg2 import errorcodes

from app.db import get_conn

from .errors import InvalidOtpError, InvalidTokenError, UserAlreadyExistsError


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def get_user_by_email(email: str) -> dict | None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM finance_users WHERE LOWER(email) = %s",
            (normalize_email(email),),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM finance_users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def register_failed_login(
    user_id: int,
    *,
    now: datetime,
    maximum_attempts: int,
    locked_until: datetime,
) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT failed_login_attempts FROM finance_users WHERE id = %s FOR UPDATE",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        attempts = int(row["failed_login_attempts"]) + 1
        cur.execute(
            """
            UPDATE finance_users
               SET failed_login_attempts = %s,
                   locked_until = CASE WHEN %s >= %s THEN %s ELSE locked_until END,
                   updated_at = %s
             WHERE id = %s
            """,
            (attempts, attempts, maximum_attempts, locked_until, now, user_id),
        )
        conn.commit()


def register_successful_login(user_id: int, now: datetime) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_users
               SET failed_login_attempts = 0,
                   locked_until = NULL,
                   last_login_at = %s,
                   updated_at = %s
             WHERE id = %s
            """,
            (now, now, user_id),
        )
        conn.commit()


def create_refresh_session(
    *,
    jti: UUID,
    family_id: UUID,
    user_id: int,
    token_digest: str,
    expires_at: datetime,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finance_refresh_sessions
                (jti, family_id, user_id, token_digest, expires_at, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(jti),
                str(family_id),
                user_id,
                token_digest,
                expires_at,
                ip_address,
                user_agent,
            ),
        )
        conn.commit()


def rotate_refresh_session(
    *,
    current_jti: UUID,
    family_id: UUID,
    current_digest: str,
    expected_token_version: int,
    new_jti: UUID,
    new_digest: str,
    new_expires_at: datetime,
    now: datetime,
    ip_address: str | None,
    user_agent: str | None,
) -> dict:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.jti, s.family_id, s.user_id, s.token_digest,
                   s.expires_at AS session_expires_at, s.revoked_at,
                   u.email, u.role, u.is_active, u.token_version
              FROM finance_refresh_sessions s
              JOIN finance_users u ON u.id = s.user_id
             WHERE s.jti = %s
             FOR UPDATE
            """,
            (str(current_jti),),
        )
        row = cur.fetchone()
        if not row or not hmac.compare_digest(row["token_digest"], current_digest):
            raise InvalidTokenError("Invalid refresh token")

        stored_family = UUID(str(row["family_id"]))
        if stored_family != family_id:
            raise InvalidTokenError("Invalid refresh token")

        if row["revoked_at"] is not None:
            cur.execute(
                """
                UPDATE finance_refresh_sessions
                   SET revoked_at = COALESCE(revoked_at, %s)
                 WHERE family_id = %s
                """,
                (now, str(stored_family)),
            )
            conn.commit()
            raise InvalidTokenError("Refresh token reuse detected")

        if (
            row["session_expires_at"] <= now
            or not row["is_active"]
            or int(row["token_version"]) != expected_token_version
        ):
            raise InvalidTokenError("Expired or inactive refresh session")

        cur.execute(
            """
            UPDATE finance_refresh_sessions
               SET revoked_at = %s, replaced_by = %s
             WHERE jti = %s
            """,
            (now, str(new_jti), str(current_jti)),
        )
        cur.execute(
            """
            INSERT INTO finance_refresh_sessions
                (jti, family_id, user_id, token_digest, expires_at, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(new_jti),
                str(stored_family),
                row["user_id"],
                new_digest,
                new_expires_at,
                ip_address,
                user_agent,
            ),
        )
        conn.commit()
        return {
            "id": row["user_id"],
            "email": row["email"],
            "role": row["role"],
            "is_active": row["is_active"],
            "token_version": row["token_version"],
        }


def revoke_refresh_session(jti: UUID, token_digest: str, now: datetime) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_refresh_sessions
               SET revoked_at = COALESCE(revoked_at, %s)
             WHERE jti = %s AND token_digest = %s
            """,
            (now, str(jti), token_digest),
        )
        conn.commit()


def revoke_all_refresh_sessions(user_id: int, now: datetime) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_refresh_sessions
               SET revoked_at = COALESCE(revoked_at, %s)
             WHERE user_id = %s
            """,
            (now, user_id),
        )
        conn.commit()


def revoke_all_sessions_and_increment_token_version(user_id: int, now: datetime) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_users
               SET token_version = token_version + 1, updated_at = %s
             WHERE id = %s
            """,
            (now, user_id),
        )
        cur.execute(
            """
            UPDATE finance_refresh_sessions
               SET revoked_at = COALESCE(revoked_at, %s)
             WHERE user_id = %s
            """,
            (now, user_id),
        )
        conn.commit()


def create_password_reset_challenge(
    *,
    email: str,
    challenge_id: UUID,
    otp_digest: str,
    expires_at: datetime,
    attempts: int,
    now: datetime,
    resend_cutoff: datetime,
    hourly_cutoff: datetime,
    max_hourly_requests: int,
    requested_ip: str | None,
) -> tuple[dict, bool] | None:
    """Return (user, created); created=False means the request was throttled."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM finance_users WHERE LOWER(email) = %s FOR UPDATE",
            (normalize_email(email),),
        )
        user = cur.fetchone()
        if not user or not user["is_active"]:
            return None

        cur.execute(
            """
            SELECT MAX(created_at) AS latest_created,
                   COUNT(*) FILTER (WHERE created_at >= %s) AS hourly_count
              FROM finance_password_reset_challenges
             WHERE user_id = %s
            """,
            (hourly_cutoff, user["id"]),
        )
        limits = cur.fetchone()
        latest_created = limits["latest_created"]
        if (
            (latest_created is not None and latest_created > resend_cutoff)
            or int(limits["hourly_count"]) >= max_hourly_requests
        ):
            return dict(user), False

        cur.execute(
            """
            UPDATE finance_password_reset_challenges
               SET consumed_at = %s
             WHERE user_id = %s AND consumed_at IS NULL
            """,
            (now, user["id"]),
        )
        cur.execute(
            """
            INSERT INTO finance_password_reset_challenges
                (id, user_id, otp_digest, expires_at, attempts_remaining, requested_ip)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                str(challenge_id),
                user["id"],
                otp_digest,
                expires_at,
                attempts,
                requested_ip,
            ),
        )
        conn.commit()
        return dict(user), True


def consume_password_reset_otp(
    *,
    email: str,
    otp: str,
    now: datetime,
    verify_digest: Callable[[UUID, str, str], bool],
) -> tuple[dict, UUID]:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.*, u.email, u.role, u.is_active, u.token_version
              FROM finance_password_reset_challenges c
              JOIN finance_users u ON u.id = c.user_id
             WHERE LOWER(u.email) = %s AND c.consumed_at IS NULL
             ORDER BY c.created_at DESC
             LIMIT 1
             FOR UPDATE
            """,
            (normalize_email(email),),
        )
        row = cur.fetchone()
        if (
            not row
            or not row["is_active"]
            or row["expires_at"] <= now
            or row["attempts_remaining"] <= 0
        ):
            if row:
                cur.execute(
                    "UPDATE finance_password_reset_challenges SET consumed_at = %s WHERE id = %s",
                    (now, row["id"]),
                )
                conn.commit()
            raise InvalidOtpError("Invalid or expired OTP")

        challenge_id = UUID(str(row["id"]))
        if not verify_digest(challenge_id, otp, row["otp_digest"]):
            attempts_left = int(row["attempts_remaining"]) - 1
            cur.execute(
                """
                UPDATE finance_password_reset_challenges
                   SET attempts_remaining = %s,
                       consumed_at = CASE WHEN %s <= 0 THEN %s ELSE consumed_at END
                 WHERE id = %s
                """,
                (attempts_left, attempts_left, now, str(challenge_id)),
            )
            conn.commit()
            raise InvalidOtpError("Invalid or expired OTP")

        cur.execute(
            "UPDATE finance_password_reset_challenges SET consumed_at = %s WHERE id = %s",
            (now, str(challenge_id)),
        )
        conn.commit()
        return (
            {
                "id": row["user_id"],
                "email": row["email"],
                "role": row["role"],
                "is_active": row["is_active"],
                "token_version": row["token_version"],
            },
            challenge_id,
        )


def reset_password(
    *, user_id: int, expected_token_version: int, password_hash: str, now: datetime
) -> dict:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM finance_users WHERE id = %s FOR UPDATE", (user_id,))
        user = cur.fetchone()
        if (
            not user
            or not user["is_active"]
            or int(user["token_version"]) != expected_token_version
        ):
            raise InvalidTokenError("Invalid or already used password reset token")

        cur.execute(
            """
            UPDATE finance_users
               SET password_hash = %s,
                   token_version = token_version + 1,
                   failed_login_attempts = 0,
                   locked_until = NULL,
                   updated_at = %s
             WHERE id = %s
            RETURNING *
            """,
            (password_hash, now, user_id),
        )
        updated = dict(cur.fetchone())
        cur.execute(
            """
            UPDATE finance_refresh_sessions
               SET revoked_at = COALESCE(revoked_at, %s)
             WHERE user_id = %s
            """,
            (now, user_id),
        )
        conn.commit()
        return updated


def create_finance_user(email: str, password_hash: str, role: str) -> dict:
    try:
        with closing(get_conn()) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO finance_users (email, password_hash, role)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (normalize_email(email), password_hash, role),
            )
            user = dict(cur.fetchone())
            conn.commit()
            return user
    except psycopg2.IntegrityError as exc:
        if exc.pgcode == errorcodes.UNIQUE_VIOLATION:
            raise UserAlreadyExistsError(
                "A Finanz-Cockpit user with this email already exists"
            ) from exc
        raise


def update_finance_user_password(
    email: str, password_hash: str, role: str | None = None
) -> dict | None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_users
               SET password_hash = %s,
                   role = COALESCE(%s, role),
                   token_version = token_version + 1,
                   failed_login_attempts = 0,
                   locked_until = NULL,
                   updated_at = NOW()
             WHERE LOWER(email) = %s
            RETURNING *
            """,
            (password_hash, role, normalize_email(email)),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE finance_refresh_sessions
                   SET revoked_at = COALESCE(revoked_at, NOW())
                 WHERE user_id = %s
                """,
                (row["id"],),
            )
        conn.commit()
        return dict(row) if row else None


def write_audit_log(
    *,
    event: str,
    success: bool,
    user_id: int | None,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO finance_auth_audit_logs
                (user_id, event, success, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, event, success, ip_address, user_agent),
        )
        conn.commit()
