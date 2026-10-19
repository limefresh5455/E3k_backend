import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app import config

from .errors import FinanceAuthConfigurationError

logger = logging.getLogger("finance_auth.email")


def validate_email_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", config.SMTP_HOST),
            ("SMTP_FROM_EMAIL", config.SMTP_FROM_EMAIL),
        )
        if not value
    ]
    if missing:
        raise FinanceAuthConfigurationError(
            f"Missing outgoing email configuration: {', '.join(missing)}"
        )
    if config.SMTP_USE_SSL and config.SMTP_USE_TLS:
        raise FinanceAuthConfigurationError(
            "Only one of SMTP_USE_SSL and SMTP_USE_TLS can be enabled"
        )
    if bool(config.SMTP_USERNAME) != bool(config.SMTP_PASSWORD):
        raise FinanceAuthConfigurationError(
            "SMTP_USERNAME and SMTP_PASSWORD must either both be set or both be empty"
        )


def send_password_reset_otp(recipient: str, otp: str) -> None:
    validate_email_configuration()
    message = EmailMessage()
    message["Subject"] = "Your Finanz-Cockpit password reset code"
    message["From"] = formataddr((config.SMTP_FROM_NAME, config.SMTP_FROM_EMAIL))
    message["To"] = recipient
    message.set_content(
        "Your Finanz-Cockpit password reset code is:\n\n"
        f"{otp}\n\n"
        f"This code expires in {config.FINANCE_OTP_EXPIRE_MINUTES} minutes. "
        "If you did not request a password reset, you can ignore this email."
    )

    context = ssl.create_default_context()
    if config.SMTP_USE_SSL:
        server_context = smtplib.SMTP_SSL(
            config.SMTP_HOST,
            config.SMTP_PORT,
            timeout=config.SMTP_TIMEOUT_SECONDS,
            context=context,
        )
    else:
        server_context = smtplib.SMTP(
            config.SMTP_HOST,
            config.SMTP_PORT,
            timeout=config.SMTP_TIMEOUT_SECONDS,
        )

    with server_context as server:
        if config.SMTP_USE_TLS:
            server.starttls(context=context)
        if config.SMTP_USERNAME:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(message)


def send_password_reset_otp_safely(recipient: str, otp: str) -> None:
    """Background-task boundary that prevents SMTP failures leaking to callers."""
    try:
        send_password_reset_otp(recipient, otp)
    except Exception:
        logger.exception("Unable to deliver Finanz-Cockpit password reset OTP")
