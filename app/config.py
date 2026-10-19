import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _read_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PCLOUD_CODE = os.getenv("PCLOUD_CODE", "kZJYSGZ8TaSQch9Ivb9ov25SMaKfmHODDvy")
PCLOUD_BASE_URL = os.getenv("PCLOUD_BASE_URL", "https://eapi.pcloud.com")
DATABASE_URL = os.getenv("DATABASE_URL")

JWT_SECRET = os.getenv("JWT_SECRET", "supersecretkey_change_in_prod12345567899878788678")
JWT_ALGO = "HS256"
JWT_EXPIRE_HOURS = 24

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin@123")

# Finanz-Cockpit authentication is intentionally configured separately from the
# legacy admin login above. Secrets have no insecure defaults; the Finanz auth
# endpoints report a configuration error until production values are supplied.
FINANCE_JWT_ACCESS_SECRET = os.getenv("FINANCE_JWT_ACCESS_SECRET", "")
FINANCE_JWT_REFRESH_SECRET = os.getenv("FINANCE_JWT_REFRESH_SECRET", "")
FINANCE_JWT_ALGORITHM = "HS256"
FINANCE_JWT_ISSUER = os.getenv("FINANCE_JWT_ISSUER", "e3k-backend")
FINANCE_JWT_AUDIENCE = os.getenv("FINANCE_JWT_AUDIENCE", "e3k-finanz-cockpit")
FINANCE_ACCESS_TOKEN_MINUTES = _read_bounded_int(
    "FINANCE_ACCESS_TOKEN_MINUTES", 15, 5, 60
)
FINANCE_REFRESH_TOKEN_DAYS = _read_bounded_int(
    "FINANCE_REFRESH_TOKEN_DAYS", 7, 1, 30
)
FINANCE_RESET_TOKEN_MINUTES = _read_bounded_int(
    "FINANCE_RESET_TOKEN_MINUTES", 10, 5, 30
)
FINANCE_OTP_PEPPER = os.getenv("FINANCE_OTP_PEPPER", "")
FINANCE_OTP_EXPIRE_MINUTES = _read_bounded_int(
    "FINANCE_OTP_EXPIRE_MINUTES", 5, 2, 15
)
FINANCE_OTP_MAX_ATTEMPTS = _read_bounded_int(
    "FINANCE_OTP_MAX_ATTEMPTS", 5, 3, 10
)
FINANCE_OTP_RESEND_SECONDS = _read_bounded_int(
    "FINANCE_OTP_RESEND_SECONDS", 60, 30, 300
)
FINANCE_OTP_MAX_REQUESTS_PER_HOUR = _read_bounded_int(
    "FINANCE_OTP_MAX_REQUESTS_PER_HOUR", 5, 1, 20
)
FINANCE_LOGIN_MAX_ATTEMPTS = _read_bounded_int(
    "FINANCE_LOGIN_MAX_ATTEMPTS", 5, 3, 20
)
FINANCE_LOGIN_LOCK_MINUTES = _read_bounded_int(
    "FINANCE_LOGIN_LOCK_MINUTES", 15, 1, 120
)
FINANCE_REFRESH_COOKIE_NAME = os.getenv(
    "FINANCE_REFRESH_COOKIE_NAME", "finance_refresh_token"
)
FINANCE_COOKIE_SECURE = os.getenv("FINANCE_COOKIE_SECURE", "true").lower() == "true"
FINANCE_COOKIE_SAMESITE = os.getenv("FINANCE_COOKIE_SAMESITE", "lax").lower()
CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = _read_bounded_int("SMTP_PORT", 587, 1, 65535)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "").strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "E3k Finanz-Cockpit").strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
SMTP_TIMEOUT_SECONDS = _read_bounded_int("SMTP_TIMEOUT_SECONDS", 15, 5, 60)

LOCAL_PDF_MODE = os.getenv("LOCAL_PDF_MODE", "false").lower() == "true"
LOCAL_PDF_FOLDER = os.getenv("LOCAL_PDF_FOLDER", "temp_pdfs")

API_TITLE = "Order Extractor API"
API_VERSION = "5.0.0"

# IMAP-to-pCloud email automation. This feature remains disabled until its
# credentials are explicitly configured in the environment.
EMAIL_AUTOMATION_ENABLED = os.getenv("EMAIL_AUTOMATION_ENABLED", "false").lower() == "true"
EMAIL_AUTOMATION_SCHEDULE_HOUR = _read_bounded_int(
    "EMAIL_AUTOMATION_SCHEDULE_HOUR", 5, 0, 23
)
EMAIL_AUTOMATION_SCHEDULE_MINUTE = _read_bounded_int(
    "EMAIL_AUTOMATION_SCHEDULE_MINUTE", 0, 0, 59
)
EMAIL_AUTOMATION_SECOND_SCHEDULE_HOUR = _read_bounded_int(
    "EMAIL_AUTOMATION_SECOND_SCHEDULE_HOUR", 12, 0, 23
)
EMAIL_AUTOMATION_SECOND_SCHEDULE_MINUTE = _read_bounded_int(
    "EMAIL_AUTOMATION_SECOND_SCHEDULE_MINUTE", 30, 0, 59
)

IMAP_HOST = os.getenv("IMAP_HOST", "").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USERNAME = os.getenv("IMAP_USERNAME", "").strip()
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
IMAP_OAUTH2_TOKEN = os.getenv("IMAP_OAUTH2_TOKEN", "")
IMAP_AUTH_METHOD = os.getenv("IMAP_AUTH_METHOD", "password").strip().lower()
IMAP_FOLDER = os.getenv("IMAP_FOLDER", "INBOX").strip()
IMAP_USE_SSL = os.getenv("IMAP_USE_SSL", "true").lower() == "true"
IMAP_TIMEOUT_SECONDS = int(os.getenv("IMAP_TIMEOUT_SECONDS", "60"))

PCLOUD_API_HOST = os.getenv("PCLOUD_API_HOST", "").strip().rstrip("/")
PCLOUD_ACCESS_TOKEN = os.getenv("PCLOUD_ACCESS_TOKEN", "")
PCLOUD_ROOT_FOLDER_ID = int(os.getenv("PCLOUD_ROOT_FOLDER_ID", "0"))
PCLOUD_TIMEOUT_SECONDS = int(os.getenv("PCLOUD_TIMEOUT_SECONDS", "60"))

ERP_BASE_URL = os.getenv("ERP_BASE_URL", "https://e3k.teboag.ch:4433/e3k.Web")
ERP_PASSWORD = os.getenv("ERP_PASSWORD")
ERP_USERNAME = os.getenv("ERP_USERNAME")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EMAIL_AUTOMATION_EXCEL_FILE = Path(
    os.getenv(
        "EMAIL_AUTOMATION_EXCEL_FILE",
        str(PROJECT_ROOT / "data" / "email_automation" / "alle-adressen1.xlsx"),
    )
)
EMAIL_AUTOMATION_LOG_FILE = Path(
    os.getenv(
        "EMAIL_AUTOMATION_LOG_FILE",
        str(PROJECT_ROOT / "logs" / "email_automation.log"),
    )
)
EMAIL_AUTOMATION_PATH_LOG_FILE = Path(
    os.getenv(
        "EMAIL_AUTOMATION_PATH_LOG_FILE",
        str(PROJECT_ROOT / "logs" / "email_automation_saved_paths.txt"),
    )
)
SUPPLIER_WORKBOOK_PATH = Path(
    os.getenv(
        "SUPPLIER_WORKBOOK_PATH",
        str(PROJECT_ROOT / "data" / "supplier address notebook.xlsx"),
    )
)
SUPPLIER_WORKBOOK_TEMPLATE_PATH = Path(
    os.getenv(
        "SUPPLIER_WORKBOOK_TEMPLATE_PATH",
        str(PROJECT_ROOT / "app" / "supplier_addresses" / "templates" / "supplier address notebook.xlsx"),
    )
)


def _resolve_tesseract_cmd() -> str:
    env_cmd = os.getenv("TESSERACT_CMD", "").strip()
    if env_cmd:
        return env_cmd

    found = shutil.which("tesseract")
    if found:
        return found

    windows_default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(windows_default):
        return windows_default

    linux_default = "/usr/bin/tesseract"
    if os.path.exists(linux_default):
        return linux_default

    return ""


TESSERACT_CMD = _resolve_tesseract_cmd()
