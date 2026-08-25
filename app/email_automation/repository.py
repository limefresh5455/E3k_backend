"""PostgreSQL-backed idempotency records for pCloud uploads."""

from app.db import get_conn


class UploadHistory:
    def __init__(self, connection=None):
        self.connection = connection or get_conn()
        self.owns_connection = connection is None

    def is_message_processed(self, message_key: str) -> bool:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM email_automation_processed_messages WHERE message_key = %s",
                (message_key,),
            )
            return cur.fetchone() is not None

    def is_attachment_processed(self, message_key: str, attachment_key: str) -> bool:
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM email_automation_processed_attachments
                   WHERE message_key = %s AND attachment_key = %s""",
                (message_key, attachment_key),
            )
            return cur.fetchone() is not None

    def find_upload(self, supplier: str, sha256: str, sha1: str = ""):
        with self.connection.cursor() as cur:
            if sha1:
                cur.execute(
                    """SELECT * FROM email_automation_uploads
                       WHERE supplier = %s
                         AND (sha256 = %s OR (sha256 LIKE 'sha1:%%' AND sha1 = %s))
                       LIMIT 1""",
                    (supplier, sha256, sha1),
                )
            else:
                cur.execute(
                    """SELECT * FROM email_automation_uploads
                       WHERE supplier = %s AND sha256 = %s""",
                    (supplier, sha256),
                )
            return cur.fetchone()

    def record_upload(
        self,
        sha256: str,
        sha1: str,
        original_filename: str,
        supplier: str,
        cloud_path: str,
        pcloud_file_id: int,
        size: int,
    ) -> None:
        self._write(
            """INSERT INTO email_automation_uploads
               (supplier, sha256, sha1, original_filename, cloud_path,
                pcloud_file_id, size)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (supplier, sha256) DO NOTHING""",
            (supplier, sha256, sha1, original_filename, cloud_path, pcloud_file_id, size),
        )

    def record_attachment(
        self,
        message_key: str,
        attachment_key: str,
        sha256: str,
        cloud_path: str,
    ) -> None:
        self._write(
            """INSERT INTO email_automation_processed_attachments
               (message_key, attachment_key, sha256, cloud_path)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (message_key, attachment_key) DO UPDATE SET
                   sha256 = EXCLUDED.sha256,
                   cloud_path = EXCLUDED.cloud_path,
                   processed_at = NOW()""",
            (message_key, attachment_key, sha256, cloud_path),
        )

    def record_message(self, message_key: str) -> None:
        self._write(
            """INSERT INTO email_automation_processed_messages (message_key)
               VALUES (%s)
               ON CONFLICT (message_key) DO UPDATE SET processed_at = NOW()""",
            (message_key,),
        )

    def _write(self, query: str, parameters: tuple) -> None:
        try:
            with self.connection.cursor() as cur:
                cur.execute(query, parameters)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        if self.owns_connection:
            self.connection.close()
