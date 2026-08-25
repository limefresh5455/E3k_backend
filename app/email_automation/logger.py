"""Logging configuration for manual and scheduled email automation runs."""

import logging

from app.config import EMAIL_AUTOMATION_LOG_FILE


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("email_automation")
    logger.setLevel(logging.INFO)

    if not any(getattr(handler, "_email_automation_file", False) for handler in logger.handlers):
        try:
            EMAIL_AUTOMATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(EMAIL_AUTOMATION_LOG_FILE, encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            file_handler._email_automation_file = True
            logger.addHandler(file_handler)
        except OSError:
            logger.exception("Email automation file logging is unavailable")
    return logger
