"""Validation for email automation settings owned by the main application."""

from urllib.parse import urlparse

from app.config import (
    EMAIL_AUTOMATION_EXCEL_FILE,
    IMAP_AUTH_METHOD,
    IMAP_FOLDER,
    IMAP_HOST,
    IMAP_OAUTH2_TOKEN,
    IMAP_PASSWORD,
    IMAP_PORT,
    IMAP_TIMEOUT_SECONDS,
    IMAP_USERNAME,
    IMAP_USE_SSL,
    PCLOUD_ACCESS_TOKEN,
    PCLOUD_API_HOST,
    PCLOUD_ROOT_FOLDER_ID,
    PCLOUD_TIMEOUT_SECONDS,
)


def validate_imap_configuration() -> list[str]:
    errors = []
    if not IMAP_HOST:
        errors.append("IMAP_HOST is required")
    if not IMAP_USERNAME:
        errors.append("IMAP_USERNAME is required")
    if IMAP_AUTH_METHOD not in {"password", "xoauth2"}:
        errors.append("IMAP_AUTH_METHOD must be 'password' or 'xoauth2'")
    if IMAP_AUTH_METHOD == "password" and not IMAP_PASSWORD:
        errors.append("IMAP_PASSWORD is required for password authentication")
    if IMAP_AUTH_METHOD == "xoauth2" and not IMAP_OAUTH2_TOKEN:
        errors.append("IMAP_OAUTH2_TOKEN is required for XOAUTH2 authentication")
    if not IMAP_FOLDER:
        errors.append("IMAP_FOLDER is required")
    if not 1 <= IMAP_PORT <= 65535:
        errors.append("IMAP_PORT must be between 1 and 65535")
    if IMAP_TIMEOUT_SECONDS <= 0:
        errors.append("IMAP_TIMEOUT_SECONDS must be positive")
    if not IMAP_USE_SSL and IMAP_AUTH_METHOD == "password":
        errors.append("Password IMAP authentication requires IMAP_USE_SSL=true")
    return errors


def validate_pcloud_configuration() -> list[str]:
    errors = []
    parsed_host = urlparse(PCLOUD_API_HOST)
    try:
        has_port = parsed_host.port is not None
    except ValueError:
        has_port = True
    if (
        parsed_host.scheme != "https"
        or parsed_host.hostname not in {"api.pcloud.com", "eapi.pcloud.com"}
        or parsed_host.path not in {"", "/"}
        or parsed_host.query
        or parsed_host.fragment
        or parsed_host.username
        or parsed_host.password
        or has_port
    ):
        errors.append("PCLOUD_API_HOST must be https://api.pcloud.com or https://eapi.pcloud.com")
    if not PCLOUD_ACCESS_TOKEN:
        errors.append("PCLOUD_ACCESS_TOKEN is required")
    if PCLOUD_ROOT_FOLDER_ID <= 0:
        errors.append("PCLOUD_ROOT_FOLDER_ID must identify a destination folder")
    if PCLOUD_TIMEOUT_SECONDS <= 0:
        errors.append("PCLOUD_TIMEOUT_SECONDS must be positive")
    return errors


def validate_configuration() -> list[str]:
    errors = validate_imap_configuration() + validate_pcloud_configuration()
    if not EMAIL_AUTOMATION_EXCEL_FILE.is_file():
        errors.append(
            f"Supplier Excel file does not exist: {EMAIL_AUTOMATION_EXCEL_FILE}"
        )
    return errors
