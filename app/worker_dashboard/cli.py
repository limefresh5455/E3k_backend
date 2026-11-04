"""Secure bootstrap utility for dashboard users and access scopes.

Examples:
    python -m app.worker_dashboard.cli --email user@example.com \
        --display-name "Example User" --permission worker.read:tebo

Add --create to create a missing identity.  The password is read securely from
the terminal and is never accepted as a command-line argument.
"""

from __future__ import annotations

import argparse
from getpass import getpass

from app.finance_auth import repository as auth_repository
from app.finance_auth.database import init_finance_auth_db
from app.finance_auth.security import hash_password

from .constants import GLOBAL_SCOPE, PERMISSIONS, USERS_MANAGE
from .database import init_worker_dashboard_db
from .repository import replace_user_access


def _parse_permission(value: str) -> tuple[str, str]:
    try:
        permission, scope = value.rsplit(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "permission must look like worker.read:tebo"
        ) from exc
    if permission not in PERMISSIONS:
        raise argparse.ArgumentTypeError(f"unsupported permission: {permission}")
    if permission == USERS_MANAGE and scope != GLOBAL_SCOPE:
        raise argparse.ArgumentTypeError("users.manage must use scope *")
    if permission != USERS_MANAGE and scope not in {"tebo", "ssb"}:
        raise argparse.ArgumentTypeError("read scope must be tebo or ssb")
    return permission, scope


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure dashboard user access")
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument(
        "--permission",
        action="append",
        type=_parse_permission,
        required=True,
        help="Repeat as needed, for example finance.read:tebo or users.manage:*",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the identity when the email does not already exist",
    )
    args = parser.parse_args()

    init_finance_auth_db()
    init_worker_dashboard_db()
    user = auth_repository.get_user_by_email(args.email, "worker")
    if not user:
        if not args.create:
            parser.error("user does not exist; add --create to create it")
        password = getpass("New dashboard password: ")
        confirmation = getpass("Confirm password: ")
        if password != confirmation:
            parser.error("passwords do not match")
        entitlements = set(args.permission)
        role = (
            "admin"
            if (USERS_MANAGE, GLOBAL_SCOPE) in entitlements
            else "user"
        )
        user = auth_repository.create_finance_user(
            args.email,
            hash_password(password),
            role,
            "worker",
        )

    replace_user_access(
        user["id"],
        display_name=args.display_name,
        entitlements=set(args.permission),
    )
    print(f"Configured dashboard access for {user['email']} (user_id={user['id']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
