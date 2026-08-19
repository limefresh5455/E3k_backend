from typing import Any, Optional

import psycopg2
from psycopg2.extras import execute_values

from app.db import get_conn
from app.supplier_addresses.errors import (
    AddressNotFoundError,
    SupplierConflictError,
    SupplierNotFoundError,
)


SUPPLIER_COLUMNS = "supplier_number, name, code_1, code_2, code_3, code_4, created_at, updated_at"
ADDRESS_COLUMNS = """
    id, position, department_attention, street, postal_code, city, country,
    business_phone, private_phone, mobile, fax, email, created_at, updated_at
"""
ADDRESS_FIELDS = (
    "department_attention", "street", "postal_code", "city", "country",
    "business_phone", "private_phone", "mobile", "fax", "email",
)


def _as_dict(row) -> dict[str, Any]:
    return dict(row)


def get_supplier(supplier_number: str, *, conn=None) -> Optional[dict[str, Any]]:
    owns_conn = conn is None
    conn = conn or get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {SUPPLIER_COLUMNS} FROM supplier_address_suppliers WHERE supplier_number = %s",
                (supplier_number,),
            )
            supplier = cur.fetchone()
            if supplier is None:
                return None
            result = _as_dict(supplier)
            cur.execute(
                f"""SELECT {ADDRESS_COLUMNS} FROM supplier_address_addresses
                    WHERE supplier_number = %s ORDER BY position, id""",
                (supplier_number,),
            )
            result["addresses"] = [_as_dict(row) for row in cur.fetchall()]
            return result
    finally:
        if owns_conn:
            conn.close()


def list_suppliers(search: Optional[str]) -> dict[str, Any]:
    conn = get_conn()
    try:
        pattern = f"%{search}%" if search else None
        where = "WHERE s.supplier_number ILIKE %s OR s.name ILIKE %s" if search else ""
        params = (pattern, pattern) if search else ()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM supplier_address_suppliers s {where}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"""
                SELECT s.supplier_number, s.name, s.code_1, s.code_2, s.code_3, s.code_4,
                       COUNT(a.id)::INTEGER AS address_count, s.updated_at
                FROM supplier_address_suppliers s
                LEFT JOIN supplier_address_addresses a ON a.supplier_number = s.supplier_number
                {where}
                GROUP BY s.supplier_number
                ORDER BY LOWER(s.name) NULLS LAST, s.supplier_number
                """,
                params,
            )
            return {"items": [_as_dict(row) for row in cur.fetchall()], "total": total}
    finally:
        conn.close()


def create_supplier(data: dict[str, Any]) -> dict[str, Any]:
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO supplier_address_suppliers
                        (supplier_number, name, code_1, code_2, code_3, code_4)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    tuple(data.get(key) for key in ("supplier_number", "name", "code_1", "code_2", "code_3", "code_4")),
                )
                for position, address in enumerate(data.get("addresses", []), start=1):
                    _insert_address(cur, data["supplier_number"], position, address)
        return get_supplier(data["supplier_number"], conn=conn)
    except psycopg2.IntegrityError as exc:
        raise SupplierConflictError("Supplier number already exists") from exc
    finally:
        conn.close()


def import_suppliers(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert workbook records atomically; never overwrite dashboard edits."""
    if not records:
        return {
            "imported": 0,
            "skipped_existing": 0,
            "addresses_imported": 0,
            "inserted_supplier_numbers": [],
        }

    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                supplier_fields = ("supplier_number", "name", "code_1", "code_2", "code_3", "code_4")
                supplier_values = [
                    tuple(record.get(field) for field in supplier_fields) for record in records
                ]
                inserted_rows = execute_values(
                    cur,
                    """
                    INSERT INTO supplier_address_suppliers
                        (supplier_number, name, code_1, code_2, code_3, code_4)
                    VALUES %s
                    ON CONFLICT (supplier_number) DO NOTHING
                    RETURNING supplier_number
                    """,
                    supplier_values,
                    page_size=1_000,
                    fetch=True,
                )
                inserted_numbers = {_supplier_number_from_row(row) for row in inserted_rows}

                address_values = []
                for record in records:
                    if record["supplier_number"] not in inserted_numbers:
                        continue
                    for position, address in enumerate(record.get("addresses", []), start=1):
                        address_values.append(
                            (
                                record["supplier_number"],
                                position,
                                *(address.get(field) for field in ADDRESS_FIELDS),
                            )
                        )
                if address_values:
                    execute_values(
                        cur,
                        f"""
                        INSERT INTO supplier_address_addresses
                            (supplier_number, position, {', '.join(ADDRESS_FIELDS)})
                        VALUES %s
                        """,
                        address_values,
                        page_size=1_000,
                    )

        imported = len(inserted_numbers)
        return {
            "imported": imported,
            "skipped_existing": len(records) - imported,
            "addresses_imported": len(address_values),
            "inserted_supplier_numbers": sorted(inserted_numbers),
        }
    finally:
        conn.close()


def _supplier_number_from_row(row) -> str:
    if isinstance(row, dict):
        return row["supplier_number"]
    try:
        return row["supplier_number"]
    except (KeyError, TypeError):
        return row[0]


def update_supplier(current_number: str, changes: dict[str, Any]) -> dict[str, Any]:
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM supplier_address_suppliers WHERE supplier_number = %s FOR UPDATE",
                    (current_number,),
                )
                if cur.fetchone() is None:
                    raise SupplierNotFoundError("Supplier not found")

                scalar_changes = {key: value for key, value in changes.items() if key != "addresses"}
                new_number = scalar_changes.get("supplier_number", current_number)
                if scalar_changes:
                    assignments = ", ".join(f"{key} = %s" for key in scalar_changes)
                    cur.execute(
                        f"UPDATE supplier_address_suppliers SET {assignments}, updated_at = NOW() WHERE supplier_number = %s",
                        (*scalar_changes.values(), current_number),
                    )

                if "addresses" in changes:
                    _upsert_addresses(cur, new_number, changes["addresses"])
                    cur.execute(
                        "UPDATE supplier_address_suppliers SET updated_at = NOW() WHERE supplier_number = %s",
                        (new_number,),
                    )
        return get_supplier(new_number, conn=conn)
    except psycopg2.IntegrityError as exc:
        raise SupplierConflictError("Supplier number or address position already exists") from exc
    finally:
        conn.close()


def _insert_address(cur, supplier_number: str, position: int, address: dict[str, Any]) -> None:
    cur.execute(
        f"""
        INSERT INTO supplier_address_addresses (supplier_number, position, {', '.join(ADDRESS_FIELDS)})
        VALUES (%s, %s, {', '.join(['%s'] * len(ADDRESS_FIELDS))})
        """,
        (supplier_number, position, *(address.get(field) for field in ADDRESS_FIELDS)),
    )


def _upsert_addresses(cur, supplier_number: str, addresses: list[dict[str, Any]]) -> None:
    cur.execute(
        "SELECT COALESCE(MAX(position), 0) AS max_position FROM supplier_address_addresses WHERE supplier_number = %s",
        (supplier_number,),
    )
    next_position = cur.fetchone()["max_position"] + 1

    for address in addresses:
        address_id = address.get("id")
        values = {field: address.get(field) for field in ADDRESS_FIELDS if field in address}
        if address_id is None:
            _insert_address(cur, supplier_number, next_position, address)
            next_position += 1
            continue
        cur.execute(
            "SELECT 1 FROM supplier_address_addresses WHERE id = %s AND supplier_number = %s",
            (address_id, supplier_number),
        )
        if cur.fetchone() is None:
            raise AddressNotFoundError(f"Address {address_id} does not belong to this supplier")
        if values:
            assignments = ", ".join(f"{field} = %s" for field in values)
            cur.execute(
                f"UPDATE supplier_address_addresses SET {assignments}, updated_at = NOW() WHERE id = %s",
                (*values.values(), address_id),
            )
