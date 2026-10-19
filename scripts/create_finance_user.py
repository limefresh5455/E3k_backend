"""Create or deliberately reset a Finanz-Cockpit user.

Usage from the project root:
    python scripts/create_finance_user.py
    python scripts/create_finance_user.py --update-existing

The password is read interactively so it is not exposed in shell history.
"""

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.finance_auth import repository  # noqa: E402
from app.finance_auth.database import init_finance_auth_db  # noqa: E402
from app.finance_auth.errors import PasswordPolicyError  # noqa: E402
from app.finance_auth.schemas import ForgotPasswordRequest  # noqa: E402
from app.finance_auth.security import hash_password  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Finanz-Cockpit user")
    parser.add_argument("--email", default="r.kurmann@tebo.ag")
    parser.add_argument(
        "--role",
        choices=("finance_user", "finance_admin"),
        default="finance_admin",
        help="Bootstrap role (default: finance_admin, required to register users)",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Reset the password if this Finanz-Cockpit user already exists",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        email = str(ForgotPasswordRequest(email=args.email).email)
    except ValidationError as exc:
        print(f"Invalid email address: {exc}", file=sys.stderr)
        return 2

    init_finance_auth_db()
    existing = repository.get_user_by_email(email)
    if existing and not args.update_existing:
        print(
            "User already exists. Use --update-existing to deliberately reset its password.",
            file=sys.stderr,
        )
        return 1

    password = getpass.getpass("New password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2

    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if existing:
        repository.update_finance_user_password(email, password_hash, args.role)
        print(
            f"Updated {email} as {args.role} and revoked its existing sessions."
        )
    else:
        repository.create_finance_user(email, password_hash, args.role)
        print(f"Created {args.role} account for {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
