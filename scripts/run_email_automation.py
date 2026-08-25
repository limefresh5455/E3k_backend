"""Run the IMAP-to-pCloud automation once from the command line."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.email_automation.runner import run_email_automation


if __name__ == "__main__":
    raise SystemExit(run_email_automation())
