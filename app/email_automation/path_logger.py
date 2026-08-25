"""Write pCloud paths uploaded by each daily scheduled run."""

import logging
from datetime import datetime
from pathlib import Path


def write_daily_path_log(
    log_file: Path,
    saved_paths: list[str],
    logger: logging.Logger,
) -> None:
    if not saved_paths:
        return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    today_header = datetime.now().strftime("%d.%m.%y") + ":"
    existing_lines = []
    if log_file.exists():
        try:
            existing_lines = log_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("Could not read path log: %s", exc)

    try:
        today_index = existing_lines.index(today_header)
    except ValueError:
        existing_lines = [today_header, *saved_paths, "", *existing_lines]
    else:
        insert_at = today_index + 1
        while insert_at < len(existing_lines) and existing_lines[insert_at].strip():
            insert_at += 1
        existing_lines[insert_at:insert_at] = saved_paths

    try:
        log_file.write_text("\n".join(existing_lines), encoding="utf-8")
        logger.info("Path log updated: %s", log_file)
    except OSError as exc:
        logger.error("Failed to write path log: %s", exc)
