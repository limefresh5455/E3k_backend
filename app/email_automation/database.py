"""PostgreSQL schema and advisory lock for email automation."""

from app.db import get_conn


LOCK_NAMESPACE = 1162691377
LOCK_ID = 1


class JobAlreadyRunning(RuntimeError):
    """Raised when another process owns the email automation lock."""


def init_email_automation_db() -> None:
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS email_automation_uploads (
                        supplier         TEXT NOT NULL,
                        sha256           TEXT NOT NULL,
                        sha1             TEXT NOT NULL,
                        original_filename TEXT NOT NULL,
                        cloud_path       TEXT NOT NULL,
                        pcloud_file_id   BIGINT NOT NULL,
                        size             BIGINT NOT NULL CHECK (size >= 0),
                        uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (supplier, sha256)
                    );

                    CREATE INDEX IF NOT EXISTS idx_email_automation_uploads_supplier_sha1
                        ON email_automation_uploads (supplier, sha1);

                    CREATE TABLE IF NOT EXISTS email_automation_processed_attachments (
                        message_key    TEXT NOT NULL,
                        attachment_key TEXT NOT NULL,
                        sha256         TEXT NOT NULL,
                        cloud_path     TEXT NOT NULL,
                        processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (message_key, attachment_key)
                    );

                    CREATE TABLE IF NOT EXISTS email_automation_processed_messages (
                        message_key  TEXT PRIMARY KEY,
                        processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
    finally:
        conn.close()


class EmailAutomationLock:
    """Hold a PostgreSQL session advisory lock for one complete job run."""

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
                raise JobAlreadyRunning("Another email automation instance is already running")
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
