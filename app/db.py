import psycopg2
from psycopg2.extras import RealDictCursor

from .config import DATABASE_URL


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id              SERIAL PRIMARY KEY,
            file_id         TEXT UNIQUE NOT NULL,
            file_name       TEXT NOT NULL,
            folder_name     TEXT,
            pdf_url         TEXT,
            order_number    TEXT,
            supplier        TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            error_message   TEXT,
            extracted_json  JSONB,
            summary         JSONB,
            processed_at    TIMESTAMPTZ DEFAULT NOW(),
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            file_id      TEXT PRIMARY KEY,
            file_name    TEXT,
            processed_at TIMESTAMPTZ DEFAULT NOW()
        );

        -- Startup-safe schema sync for existing databases:
        -- ensure new columns exist even when the table was created earlier.
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS erp_record_id TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS erp_voucher_number TEXT;
        ALTER TABLE orders ADD COLUMN IF NOT EXISTS erp_supplier_number TEXT;
        """
    )
    conn.commit()
    cur.close()
    conn.close()
