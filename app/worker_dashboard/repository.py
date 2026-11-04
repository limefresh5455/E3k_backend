"""Persistence for dashboard display profiles and entitlements."""

from __future__ import annotations

from contextlib import closing

from app.db import get_conn
from psycopg2.extras import Json

from .constants import COMPANY_SCOPES, GLOBAL_SCOPE, PERMISSIONS, USERS_MANAGE


def get_profile_and_entitlements(user_id: int) -> tuple[str | None, set[tuple[str, str]]]:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT display_name FROM dashboard_user_profiles WHERE user_id = %s",
            (user_id,),
        )
        profile = cur.fetchone()
        cur.execute(
            """
            SELECT permission, company_scope
              FROM dashboard_entitlements
             WHERE user_id = %s
            """,
            (user_id,),
        )
        entitlements = {
            (row["permission"], row["company_scope"]) for row in cur.fetchall()
        }
    return (profile["display_name"] if profile else None), entitlements


def replace_user_access(
    user_id: int,
    *,
    display_name: str,
    entitlements: set[tuple[str, str]],
) -> None:
    cleaned_name = display_name.strip()
    if not cleaned_name:
        raise ValueError("display_name must not be empty")
    _validate_entitlements(entitlements)
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM finance_users WHERE id = %s FOR UPDATE", (user_id,))
        if not cur.fetchone():
            raise ValueError("user does not exist")
        cur.execute(
            """
            INSERT INTO dashboard_user_profiles (user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    updated_at = NOW()
            """,
            (user_id, cleaned_name),
        )
        cur.execute("DELETE FROM dashboard_entitlements WHERE user_id = %s", (user_id,))
        if entitlements:
            cur.executemany(
                """
                INSERT INTO dashboard_entitlements
                    (user_id, permission, company_scope)
                VALUES (%s, %s, %s)
                """,
                [(user_id, permission, scope) for permission, scope in sorted(entitlements)],
            )
        conn.commit()


def list_dashboard_users() -> list[dict]:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.email, u.is_active,
                   COALESCE(p.display_name, u.email) AS display_name,
                   COALESCE(
                       JSON_AGG(
                           JSON_BUILD_OBJECT(
                               'permission', e.permission,
                               'company_scope', e.company_scope
                           ) ORDER BY e.permission, e.company_scope
                       ) FILTER (WHERE e.permission IS NOT NULL),
                       '[]'::json
                   ) AS entitlements
              FROM finance_users u
              LEFT JOIN dashboard_user_profiles p ON p.user_id = u.id
              LEFT JOIN dashboard_entitlements e ON e.user_id = u.id
             GROUP BY u.id, p.display_name
             ORDER BY LOWER(u.email)
            """
        )
        return [dict(row) for row in cur.fetchall()]


def set_user_active(user_id: int, active: bool) -> bool:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finance_users
               SET is_active = %s,
                   token_version = token_version + 1,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING id
            """,
            (active, user_id),
        )
        changed = cur.fetchone() is not None
        if changed:
            cur.execute(
                """
                UPDATE finance_refresh_sessions
                   SET revoked_at = COALESCE(revoked_at, NOW())
                 WHERE user_id = %s
                """,
                (user_id,),
            )
        conn.commit()
        return changed


def write_audit_log(
    *,
    actor_user_id: int | None,
    target_user_id: int | None,
    event: str,
    details: dict,
) -> None:
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dashboard_audit_logs
                (actor_user_id, target_user_id, event, details)
            VALUES (%s, %s, %s, %s)
            """,
            (actor_user_id, target_user_id, event, Json(details)),
        )
        conn.commit()


def _validate_entitlements(entitlements: set[tuple[str, str]]) -> None:
    for permission, scope in entitlements:
        if permission not in PERMISSIONS:
            raise ValueError(f"unsupported dashboard permission: {permission}")
        if permission == USERS_MANAGE:
            if scope != GLOBAL_SCOPE:
                raise ValueError("users.manage must use the global scope")
        elif scope not in COMPANY_SCOPES:
            raise ValueError(f"unsupported company scope: {scope}")
