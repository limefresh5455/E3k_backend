import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.supplier_addresses.database import init_supplier_address_db
from app.supplier_addresses.excel_import import parse_supplier_workbook
from app.supplier_addresses.repository import import_suppliers


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the ERP address-list workbook into PostgreSQL.")
    parser.add_argument("workbook", type=Path, help="Path to the .xlsx address-list export")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write to PostgreSQL. Without this flag, only parse and validate the workbook.",
    )
    args = parser.parse_args()

    if not args.workbook.is_file():
        parser.error(f"Workbook does not exist: {args.workbook}")

    records = parse_supplier_workbook(args.workbook)
    print(f"Validated {len(records)} supplier records from {args.workbook}")
    if not args.commit:
        print("Dry run only; database was not changed. Re-run with --commit to import.")
        return 0

    init_supplier_address_db()
    result = import_suppliers(records)
    print(
        f"Imported {result['imported']} suppliers; "
        f"skipped {result['skipped_existing']} existing suppliers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
