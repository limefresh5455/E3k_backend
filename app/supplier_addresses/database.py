from app.db import get_conn


def init_supplier_address_db() -> None:
    """Create only the tables owned by this feature."""
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS supplier_address_suppliers (
                        supplier_number TEXT PRIMARY KEY,
                        name            TEXT,
                        code_1          TEXT,
                        code_2          TEXT,
                        code_3          TEXT,
                        code_4          TEXT,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        CHECK (BTRIM(supplier_number) <> '')
                    );

                    CREATE TABLE IF NOT EXISTS supplier_address_addresses (
                        id                   BIGSERIAL PRIMARY KEY,
                        supplier_number      TEXT NOT NULL REFERENCES supplier_address_suppliers(supplier_number)
                                                ON UPDATE CASCADE ON DELETE RESTRICT,
                        position             INTEGER NOT NULL,
                        department_attention TEXT,
                        street               TEXT,
                        postal_code          TEXT,
                        city                 TEXT,
                        country              TEXT,
                        business_phone       TEXT,
                        private_phone        TEXT,
                        mobile               TEXT,
                        fax                  TEXT,
                        email                TEXT,
                        created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (supplier_number, position),
                        CHECK (position > 0)
                    );

                    CREATE INDEX IF NOT EXISTS idx_supplier_address_suppliers_name_lower
                        ON supplier_address_suppliers (LOWER(name));
                    CREATE INDEX IF NOT EXISTS idx_supplier_address_addresses_supplier
                        ON supplier_address_addresses (supplier_number);

                    ALTER TABLE supplier_address_suppliers ALTER COLUMN name DROP NOT NULL;
                    ALTER TABLE supplier_address_suppliers
                        DROP CONSTRAINT IF EXISTS supplier_address_suppliers_name_check;
                    """
                )
    finally:
        conn.close()
