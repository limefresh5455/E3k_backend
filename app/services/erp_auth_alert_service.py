"""Non-blocking, rate-limited email alerts for ERP authentication failures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
import logging
import smtplib
import ssl
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app import config
from app.db import get_conn


logger = logging.getLogger("erp_auth_alert")

ALERT_TYPE = "erp_401_unauthorized"
ALERT_LEASE_MINUTES = 5
CLIENT_TIMEZONE = ZoneInfo("Europe/Zurich")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erp-auth-alert")
_configuration_warning_lock = Lock()
_configuration_warning_logged = False


def _log_missing_configuration_once(message: str) -> None:
    global _configuration_warning_logged
    with _configuration_warning_lock:
        if _configuration_warning_logged:
            return
        _configuration_warning_logged = True
    logger.warning(message)


def _safe_endpoint(url: str) -> str:
    """Remove query strings, fragments, and any embedded URL credentials."""
    parsed = urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.hostname:
        return "ERP API"

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _row_value(row, key: str, index: int):
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def _cooldown_elapsed(last_sent_at: datetime | None, now: datetime) -> bool:
    if last_sent_at is None:
        return True
    if last_sent_at.tzinfo is None:
        last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
    return now - last_sent_at >= timedelta(
        hours=config.ERP_AUTH_ALERT_COOLDOWN_HOURS
    )


def _claim_alert_delivery(now: datetime | None = None) -> UUID | None:
    """Atomically claim the alert across all application processes."""
    now = now or datetime.now(timezone.utc)
    token = uuid4()
    lease_until = now + timedelta(minutes=ALERT_LEASE_MINUTES)

    with closing(get_conn()) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS erp_alert_state (
                        alert_type   TEXT PRIMARY KEY,
                        last_sent_at TIMESTAMPTZ,
                        lease_until  TIMESTAMPTZ,
                        lease_token  UUID
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO erp_alert_state (alert_type)
                    VALUES (%s)
                    ON CONFLICT (alert_type) DO NOTHING
                    """,
                    (ALERT_TYPE,),
                )
                cur.execute(
                    """
                    SELECT last_sent_at, lease_until
                      FROM erp_alert_state
                     WHERE alert_type = %s
                     FOR UPDATE
                    """,
                    (ALERT_TYPE,),
                )
                row = cur.fetchone()
                last_sent_at = _row_value(row, "last_sent_at", 0)
                current_lease_until = _row_value(row, "lease_until", 1)
                if not _cooldown_elapsed(last_sent_at, now):
                    return None
                if current_lease_until is not None:
                    if current_lease_until.tzinfo is None:
                        current_lease_until = current_lease_until.replace(
                            tzinfo=timezone.utc
                        )
                    if current_lease_until > now:
                        return None

                cur.execute(
                    """
                    UPDATE erp_alert_state
                       SET lease_until = %s,
                           lease_token = %s
                     WHERE alert_type = %s
                    """,
                    (lease_until, token, ALERT_TYPE),
                )
    return token


def _complete_alert_delivery(token: UUID, sent_at: datetime) -> None:
    with closing(get_conn()) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE erp_alert_state
                       SET last_sent_at = %s,
                           lease_until = NULL,
                           lease_token = NULL
                     WHERE alert_type = %s
                       AND lease_token = %s
                    """,
                    (sent_at, ALERT_TYPE, token),
                )


def _release_alert_delivery(token: UUID) -> None:
    with closing(get_conn()) as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE erp_alert_state
                       SET lease_until = NULL,
                           lease_token = NULL
                     WHERE alert_type = %s
                       AND lease_token = %s
                    """,
                    (ALERT_TYPE, token),
                )


def _validate_email_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("ERP_AUTH_ALERT_SMTP_HOST", config.ERP_AUTH_ALERT_SMTP_HOST),
            ("ERP_AUTH_ALERT_FROM_EMAIL", config.ERP_AUTH_ALERT_FROM_EMAIL),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing outgoing email configuration: {', '.join(missing)}"
        )
    if config.ERP_AUTH_ALERT_SMTP_USE_SSL and config.ERP_AUTH_ALERT_SMTP_USE_TLS:
        raise ValueError(
            "Only one of ERP_AUTH_ALERT_SMTP_USE_SSL and "
            "ERP_AUTH_ALERT_SMTP_USE_TLS can be enabled"
        )
    if bool(config.ERP_AUTH_ALERT_SMTP_USERNAME) != bool(
        config.ERP_AUTH_ALERT_SMTP_PASSWORD
    ):
        raise ValueError(
            "ERP_AUTH_ALERT_SMTP_USERNAME and ERP_AUTH_ALERT_SMTP_PASSWORD "
            "must either both be set or both be empty"
        )


def _send_erp_auth_email(method: str, endpoint: str, occurred_at: datetime) -> None:
    _validate_email_configuration()
    local_time = occurred_at.astimezone(CLIENT_TIMEZONE)
    message = EmailMessage()
    message["Subject"] = "[E3K Warning] ERP authentication failed - 401 Unauthorized"
    message["From"] = formataddr(
        (config.ERP_AUTH_ALERT_FROM_NAME, config.ERP_AUTH_ALERT_FROM_EMAIL)
    )
    message["To"] = ", ".join(config.ERP_AUTH_ALERT_RECIPIENTS)
    message.set_content(
        "Guten Tag,\n\n"
        "mindestens eine Anfrage unseres Servers an das E3K ERP wurde wegen "
        "eines Authentifizierungsfehlers abgelehnt.\n\n"
        f"Zeitpunkt: {local_time.strftime('%d.%m.%Y %H:%M:%S %Z')}\n"
        "Fehler: 401 Unauthorized\n"
        "Betroffener Prozess: ERP API-Anfrage\n"
        f"Anfrage: {method.upper()} {endpoint}\n\n"
        "Der bestehende Prozess läuft weiterhin gemäß seiner konfigurierten "
        "Retry-Logik. Bitte prüfen Sie den ERP-Benutzernamen, das Passwort und "
        "die Zugriffsberechtigung.\n\n"
        "Aus Sicherheitsgründen enthält diese Nachricht keine Zugangsdaten oder "
        "sensiblen Response-Daten.\n\n"
        "Diese Warnung wird maximal einmal innerhalb von 24 Stunden versendet.\n\n"
        "Freundliche Grüsse\n"
        "E3K Monitoring System\n"
    )

    context = ssl.create_default_context()
    if config.ERP_AUTH_ALERT_SMTP_USE_SSL:
        server_context = smtplib.SMTP_SSL(
            config.ERP_AUTH_ALERT_SMTP_HOST,
            config.ERP_AUTH_ALERT_SMTP_PORT,
            timeout=config.ERP_AUTH_ALERT_SMTP_TIMEOUT_SECONDS,
            context=context,
        )
    else:
        server_context = smtplib.SMTP(
            config.ERP_AUTH_ALERT_SMTP_HOST,
            config.ERP_AUTH_ALERT_SMTP_PORT,
            timeout=config.ERP_AUTH_ALERT_SMTP_TIMEOUT_SECONDS,
        )

    with server_context as server:
        if config.ERP_AUTH_ALERT_SMTP_USE_TLS:
            server.starttls(context=context)
        if config.ERP_AUTH_ALERT_SMTP_USERNAME:
            server.login(
                config.ERP_AUTH_ALERT_SMTP_USERNAME,
                config.ERP_AUTH_ALERT_SMTP_PASSWORD,
            )
        server.send_message(message)


def _process_erp_unauthorized_alert(
    method: str, endpoint: str, occurred_at: datetime
) -> None:
    token = None
    try:
        token = _claim_alert_delivery()
        if token is None:
            logger.info("ERP 401 email skipped because the 24-hour cooldown is active")
            return
        _send_erp_auth_email(method, endpoint, occurred_at)
        _complete_alert_delivery(token, datetime.now(timezone.utc))
        logger.warning("ERP 401 warning email sent to configured recipients")
    except Exception:
        logger.exception("Unable to deliver ERP 401 warning email")
        if token is not None:
            try:
                _release_alert_delivery(token)
            except Exception:
                logger.exception("Unable to release ERP 401 email delivery lease")


def schedule_erp_unauthorized_alert(method: str, url: str) -> bool:
    """Queue an ERP 401 alert without waiting for database or SMTP operations."""
    if not config.ERP_AUTH_ALERT_RECIPIENTS:
        _log_missing_configuration_once(
            "ERP 401 alert email is disabled because ERP_AUTH_ALERT_RECIPIENTS is empty"
        )
        return False

    endpoint = _safe_endpoint(url)
    occurred_at = datetime.now(timezone.utc)
    try:
        _executor.submit(
            _process_erp_unauthorized_alert,
            method,
            endpoint,
            occurred_at,
        )
    except RuntimeError:
        logger.exception("Unable to queue ERP 401 warning email")
        return False
    return True
