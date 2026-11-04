"""Database objects for dashboard profiles and scoped permissions."""

from contextlib import closing

from app.db import get_conn


LOCK_NAMESPACE = 1464091732
LOCK_ID = 1


class DashboardPublicationAlreadyRunning(RuntimeError):
    """Raised when another process owns the publication cycle."""


def init_worker_dashboard_db() -> None:
    """Create additive tables without changing the existing finance schema."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_user_profiles (
                user_id      BIGINT PRIMARY KEY
                             REFERENCES finance_users(id) ON DELETE CASCADE,
                display_name TEXT NOT NULL,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT dashboard_display_name_not_blank
                    CHECK (BTRIM(display_name) <> '')
            );

            CREATE TABLE IF NOT EXISTS dashboard_entitlements (
                user_id       BIGINT NOT NULL
                              REFERENCES finance_users(id) ON DELETE CASCADE,
                permission    TEXT NOT NULL,
                company_scope TEXT NOT NULL,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, permission, company_scope),
                CONSTRAINT dashboard_permission_check
                    CHECK (permission IN ('worker.read', 'finance.read', 'users.manage')),
                CONSTRAINT dashboard_scope_check
                    CHECK (
                        (permission IN ('worker.read', 'finance.read')
                         AND company_scope IN ('tebo', 'ssb'))
                        OR
                        (permission = 'users.manage' AND company_scope = '*')
                    )
            );

            CREATE INDEX IF NOT EXISTS idx_dashboard_entitlements_user
                ON dashboard_entitlements (user_id);

            CREATE TABLE IF NOT EXISTS dashboard_audit_logs (
                id             BIGSERIAL PRIMARY KEY,
                actor_user_id  BIGINT REFERENCES finance_users(id) ON DELETE SET NULL,
                target_user_id BIGINT REFERENCES finance_users(id) ON DELETE SET NULL,
                event          TEXT NOT NULL,
                details        JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_dashboard_audit_created
                ON dashboard_audit_logs (created_at DESC);

            -- Accounts created by the original worker dashboard were stored as
            -- finance identities. Reclassify only identities that actually own
            -- a worker profile or entitlement, invalidate their old tokens, and
            -- leave finance-only identities unchanged.
            WITH migrated AS (
                UPDATE finance_users u
                   SET dashboard_access = 'worker',
                       role = CASE
                           WHEN EXISTS (
                               SELECT 1
                                 FROM dashboard_entitlements e
                                WHERE e.user_id = u.id
                                  AND e.permission = 'users.manage'
                                  AND e.company_scope = '*'
                           ) THEN 'admin'
                           ELSE 'user'
                       END,
                       token_version = token_version + 1,
                       updated_at = NOW()
                 WHERE u.dashboard_access = 'finance'
                   AND (
                       EXISTS (
                           SELECT 1 FROM dashboard_user_profiles p
                            WHERE p.user_id = u.id
                       )
                       OR EXISTS (
                           SELECT 1 FROM dashboard_entitlements e
                            WHERE e.user_id = u.id
                       )
                   )
                RETURNING u.id
            )
            UPDATE finance_refresh_sessions s
               SET revoked_at = COALESCE(s.revoked_at, NOW())
             WHERE s.user_id IN (SELECT id FROM migrated);
            """
        )
        conn.commit()


class DashboardPublicationLock:
    """Hold a PostgreSQL advisory lock across one complete publication scan."""

    def __init__(self, connection_factory=get_conn):
        self.connection_factory = connection_factory
        self.connection = None
        self.acquired = False

    def __enter__(self):
        self.connection = self.connection_factory()
        try:
            with self.connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                    (LOCK_NAMESPACE, LOCK_ID),
                )
                row = cur.fetchone()
            self.acquired = bool(
                row.get("acquired") if isinstance(row, dict) else row[0]
            )
            if not self.acquired:
                raise DashboardPublicationAlreadyRunning(
                    "Another dashboard publication cycle is already running"
                )
            return self
        except Exception:
            if not self.acquired and self.connection is not None:
                self.connection.close()
                self.connection = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        if self.connection is None:
            return
        try:
            if self.acquired:
                with self.connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        (LOCK_NAMESPACE, LOCK_ID),
                    )
        finally:
            self.connection.close()
            self.connection = None
            self.acquired = False
