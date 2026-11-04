from contextlib import closing

from app.db import get_conn


def init_finance_auth_db() -> None:
    """Create the isolated Finanz-Cockpit authentication schema."""
    with closing(get_conn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS finance_users (
                id                    BIGSERIAL PRIMARY KEY,
                email                 TEXT NOT NULL,
                password_hash         TEXT NOT NULL,
                dashboard_access      TEXT NOT NULL DEFAULT 'finance',
                role                  TEXT NOT NULL DEFAULT 'user',
                is_active             BOOLEAN NOT NULL DEFAULT TRUE,
                token_version         INTEGER NOT NULL DEFAULT 1,
                failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until          TIMESTAMPTZ,
                last_login_at         TIMESTAMPTZ,
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT finance_users_dashboard_access_check
                    CHECK (dashboard_access IN ('finance', 'worker')),
                CONSTRAINT finance_users_dashboard_role_check
                    CHECK (role IN ('admin', 'user')),
                CONSTRAINT finance_users_email_normalized_check
                    CHECK (email = LOWER(BTRIM(email)))
            );

            -- Migrate the original finance-only identity schema in place. Existing
            -- rows remain finance accounts until the worker-dashboard migration
            -- below identifies rows that own dashboard entitlements.
            ALTER TABLE finance_users
                ADD COLUMN IF NOT EXISTS dashboard_access TEXT;
            ALTER TABLE finance_users
                DROP CONSTRAINT IF EXISTS finance_users_role_check;
            UPDATE finance_users
               SET dashboard_access = COALESCE(dashboard_access, 'finance'),
                   role = CASE role
                       WHEN 'finance_admin' THEN 'admin'
                       WHEN 'finance_user' THEN 'user'
                       ELSE role
                   END
             WHERE dashboard_access IS NULL
                OR role IN ('finance_admin', 'finance_user');
            ALTER TABLE finance_users
                ALTER COLUMN dashboard_access SET DEFAULT 'finance',
                ALTER COLUMN dashboard_access SET NOT NULL,
                ALTER COLUMN role SET DEFAULT 'user';

            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'finance_users_dashboard_access_check'
                       AND conrelid = 'finance_users'::regclass
                ) THEN
                    ALTER TABLE finance_users
                        ADD CONSTRAINT finance_users_dashboard_access_check
                        CHECK (dashboard_access IN ('finance', 'worker'));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'finance_users_dashboard_role_check'
                       AND conrelid = 'finance_users'::regclass
                ) THEN
                    ALTER TABLE finance_users
                        ADD CONSTRAINT finance_users_dashboard_role_check
                        CHECK (role IN ('admin', 'user'));
                END IF;
            END
            $$;

            DROP INDEX IF EXISTS uq_finance_users_email_lower;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_users_email_dashboard
                ON finance_users (LOWER(email), dashboard_access);

            CREATE TABLE IF NOT EXISTS finance_refresh_sessions (
                jti          UUID PRIMARY KEY,
                family_id    UUID NOT NULL,
                user_id      BIGINT NOT NULL REFERENCES finance_users(id) ON DELETE CASCADE,
                token_digest TEXT NOT NULL,
                expires_at   TIMESTAMPTZ NOT NULL,
                revoked_at   TIMESTAMPTZ,
                replaced_by  UUID,
                ip_address   TEXT,
                user_agent   TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_finance_refresh_user
                ON finance_refresh_sessions (user_id);
            CREATE INDEX IF NOT EXISTS idx_finance_refresh_family
                ON finance_refresh_sessions (family_id);

            CREATE TABLE IF NOT EXISTS finance_password_reset_challenges (
                id                 UUID PRIMARY KEY,
                user_id            BIGINT NOT NULL REFERENCES finance_users(id) ON DELETE CASCADE,
                otp_digest         TEXT NOT NULL,
                expires_at         TIMESTAMPTZ NOT NULL,
                attempts_remaining INTEGER NOT NULL,
                consumed_at        TIMESTAMPTZ,
                requested_ip       TEXT,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_finance_reset_user_created
                ON finance_password_reset_challenges (user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS finance_auth_audit_logs (
                id         BIGSERIAL PRIMARY KEY,
                user_id    BIGINT REFERENCES finance_users(id) ON DELETE SET NULL,
                event      TEXT NOT NULL,
                success    BOOLEAN NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_finance_auth_audit_created
                ON finance_auth_audit_logs (created_at DESC);
            """
        )
        conn.commit()
